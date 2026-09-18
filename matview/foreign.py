"""Foreign recipes: computations written in any language.

Unlike Python recipes, foreign recipes **declare their dependencies upfront** in a
manifest rather than discovering them at runtime. This is the privileged case, not a
degraded one: a declared graph can be cycle-checked and planned before anything
executes, whereas a Python recipe's graph is only knowable by running it. Dynamic
discovery is the escape hatch.

A manifest is data, not code, so registering a foreign recipe never requires its
language to be installed.

The invocation contract is one JSON job file passed as the **only** argument. Never
parameters on the command line, never positional input paths; both fall apart at a
dozen inputs or a nested params dict.

    {
      "params":  {"dataset": "ie-ielex", "model": "GTR"},
      "inputs":  {"matrix": "/tmp/matview-8a1f/matrix.arrow"},
      "output":  "/tmp/matview-8a1f/out.json",
      "deadline_s": 120
    }

Rules the host enforces:

- stdout and stderr are logging only. The result is read from `output`, never from
  stdout, so a stray `print` in an R script cannot corrupt a value.
- Nonzero exit, a missing output file, or an unparseable one is a compute failure and
  is not cached. A poisoned artifact from a half-written output is much worse than a
  recompute.
- The job file is left on disk on failure, so a failed job is reproducible by hand by
  rerunning the same command.
- `deadline_s` is the remaining budget from the propagated deadline, and the host also
  enforces it by killing the process.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from matview.canon import Scalar
from matview.errors import ForeignRecipeError
from matview.fingerprint import source_hash
from matview.registry import REGISTRY, ForeignInput, ForeignSpec, Recipe, Registry, register_foreign
from matview.types import OnMiss

if TYPE_CHECKING:
    from matview.cache import Cache, _Walk
    from matview.context import Context
    from matview.policy import Policy
    from matview.store.base import Store
    from matview.types import DepEdge

__all__ = [
    "check_declared_cycles",
    "load_manifest",
    "load_manifests",
    "run_foreign",
    "substitute_key",
]

log = logging.getLogger("matview.foreign")

_PARAM_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
}


# --- manifests -------------------------------------------------------------


def load_manifests(
    directory: str | Path,
    *,
    registry: Registry | None = None,
    store: Store | None = None,
) -> list[Recipe]:
    """Register every `*.toml` manifest under `directory`, by directory scan.

    Pass a `store` to have source hashes checked against what the cache last saw; a
    changed source with an unchanged version is a warning, since the artifacts already
    stored under that version were produced by different code.
    """
    target = REGISTRY if registry is None else registry
    root = Path(directory)
    if not root.is_dir():
        raise ForeignRecipeError(f"{root} is not a directory")

    recipes = [load_manifest(path, registry=target) for path in sorted(root.rglob("*.toml"))]
    check_declared_cycles(target)
    if store is not None:
        warn_on_changed_sources(store, recipes)
    return recipes


def load_manifest(path: str | Path, *, registry: Registry | None = None) -> Recipe:
    """Read one manifest and register it exactly as if a decorator had."""
    target = REGISTRY if registry is None else registry
    manifest_path = Path(path)
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ForeignRecipeError(f"{manifest_path}: unreadable manifest: {exc}") from exc

    name = _require(raw, "name", str, manifest_path)
    version = _require(raw, "version", int, manifest_path)
    codec = _require(raw, "codec", str, manifest_path)
    command = tuple(_require(raw, "command", list, manifest_path))
    if not command or not all(isinstance(part, str) for part in command):
        raise ForeignRecipeError(f"{manifest_path}: command must be a non-empty list of strings")

    index = tuple(raw.get("index", ()))
    on_miss: OnMiss = raw.get("on_miss", "compute")
    deterministic = bool(raw.get("deterministic", True))
    max_age = raw.get("max_age")
    source = raw.get("source")

    params: dict[str, dict[str, Any]] = raw.get("params", {})
    if not isinstance(params, dict):
        raise ForeignRecipeError(f"{manifest_path}: [params] must be a table")
    defaults: dict[str, Scalar] = {}
    param_types: dict[str, str] = {}
    for param, declared in params.items():
        if not isinstance(declared, dict):
            raise ForeignRecipeError(f"{manifest_path}: param {param!r} must be a table")
        kind = declared.get("type", "string")
        if kind not in _PARAM_TYPES:
            raise ForeignRecipeError(
                f"{manifest_path}: param {param!r} has unknown type {kind!r}; "
                f"known types: {', '.join(sorted(_PARAM_TYPES))}"
            )
        param_types[param] = kind
        if "default" in declared:
            defaults[param] = declared["default"]
            _check_type(manifest_path, param, declared["default"], kind)

    inputs = tuple(
        _read_input(manifest_path, entry, tuple(params)) for entry in raw.get("inputs", ())
    )

    resolved_source = None if source is None else (manifest_path.parent / source)
    hashed = None
    if resolved_source is not None:
        try:
            hashed = source_hash(resolved_source.read_bytes())
        except OSError as exc:
            raise ForeignRecipeError(
                f"{manifest_path}: declared source {source!r} is unreadable: {exc}"
            ) from exc

    spec = ForeignSpec(
        command=command,
        inputs=inputs,
        source=None if resolved_source is None else str(resolved_source),
        manifest_path=str(manifest_path),
        param_types=param_types,
        workdir=str(manifest_path.parent),
    )
    return register_foreign(
        target,
        name=name,
        version=version,
        codec=codec,
        index=index,
        on_miss=on_miss,
        deterministic=deterministic,
        max_age=max_age,
        params=tuple(params),
        defaults=defaults,
        spec=spec,
        source_hash=hashed,
    )


def _require(raw: Mapping[str, Any], field: str, kind: type, path: Path) -> Any:
    if field not in raw:
        raise ForeignRecipeError(f"{path}: manifest is missing required field {field!r}")
    value = raw[field]
    if kind is int and isinstance(value, bool):
        raise ForeignRecipeError(f"{path}: {field} must be {kind.__name__}, got a bool")
    if not isinstance(value, kind):
        raise ForeignRecipeError(
            f"{path}: {field} must be {kind.__name__}, got {type(value).__name__}"
        )
    return value


def _check_type(path: Path, param: str, value: Any, kind: str) -> None:
    expected = _PARAM_TYPES[kind]
    if kind != "bool" and isinstance(value, bool):
        raise ForeignRecipeError(f"{path}: param {param!r} declares {kind}, got a bool")
    if not isinstance(value, expected):
        raise ForeignRecipeError(
            f"{path}: param {param!r} declares {kind}, got {type(value).__name__}"
        )


def _read_input(path: Path, entry: Any, params: tuple[str, ...]) -> ForeignInput:
    if not isinstance(entry, dict):
        raise ForeignRecipeError(f"{path}: each [[inputs]] entry must be a table")
    for field in ("name", "recipe"):
        if field not in entry:
            raise ForeignRecipeError(f"{path}: an [[inputs]] entry is missing {field!r}")
    key = entry.get("key", {})
    if not isinstance(key, dict):
        raise ForeignRecipeError(f"{path}: input {entry['name']!r} has a non-table key")
    for template in key.values():
        if isinstance(template, str):
            for referenced in _referenced_params(template):
                if referenced not in params:
                    raise ForeignRecipeError(
                        f"{path}: input {entry['name']!r} refers to ${{{referenced}}}, which "
                        f"is not a parameter of this recipe"
                    )
    return ForeignInput(
        name=str(entry["name"]),
        recipe=str(entry["recipe"]),
        key=key,
        part=entry.get("part"),
    )


def _referenced_params(template: str) -> list[str]:
    out = []
    rest = template
    while "${" in rest:
        _, _, rest = rest.partition("${")
        name, sep, rest = rest.partition("}")
        if sep:
            out.append(name)
    return out


def check_declared_cycles(registry: Registry) -> None:
    """Cycle-check the declared graph before anything executes.

    Only foreign recipes appear here, because only they declare their inputs. A cycle
    that runs through a Python recipe is caught at runtime by the in-flight stack
    instead.
    """
    edges = {
        rec.name: [i.recipe for i in rec.foreign.inputs]
        for rec in registry
        if rec.foreign is not None
    }
    state: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == 1:
            raise ForeignRecipeError("declared dependency cycle: " + " -> ".join([*path, name]))
        if state.get(name) == 2:
            return
        state[name] = 1
        for nxt in edges.get(name, ()):
            visit(nxt, [*path, name])
        state[name] = 2

    for name in edges:
        visit(name, [])


def warn_on_changed_sources(store: Store, recipes: Sequence[Recipe]) -> list[str]:
    """Warn when a source file changed without a version bump.

    Because a foreign recipe is usually a single file rather than a package with an
    import graph, hashing its source is reliable here in a way it is not for Python.
    The hash is advisory and never fingerprinted: it cannot invalidate anything, it can
    only tell you that the artifacts already stored under this version were produced by
    different code.
    """
    changed = []
    for rec in recipes:
        if rec.source_hash is None:
            continue
        recorded = store.recipe_source_hash(rec.name, rec.version)  # type: ignore[attr-defined]
        if recorded is not None and recorded != rec.source_hash:
            message = (
                f"{rec.name} v{rec.version}: source changed but the version did not. "
                f"Artifacts already stored under v{rec.version} came from different code."
            )
            changed.append(message)
            log.warning("%s", message)
    return changed


# --- invocation ------------------------------------------------------------


def run_foreign(
    cache: Cache,
    walk: _Walk,
    rec: Recipe,
    key: Mapping[str, Scalar],
    ctx: Context,
    collected: list[DepEdge],
    policy: Policy,
) -> Any:
    """Resolve declared inputs, materialize them, invoke, and read the result."""
    spec = rec.foreign
    assert spec is not None

    for param, kind in spec.param_types.items():
        if param in key:
            _check_type(Path(spec.manifest_path or rec.name), param, key[param], kind)

    workdir = Path(tempfile.mkdtemp(prefix=f"matview-{rec.name}-"))
    succeeded = False
    try:
        inputs = {
            declared.name: _materialize(cache, ctx, rec, declared, key, workdir)
            for declared in spec.inputs
        }
        output = workdir / "output"
        job = {
            "params": dict(key),
            "inputs": {name: str(path) for name, path in inputs.items()},
            "output": str(output),
            "deadline_s": policy.remaining(),
        }
        job_file = workdir / "job.json"
        job_file.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")

        value = _invoke(rec, spec, job_file, output, policy)
        succeeded = True
        return value
    finally:
        # The job file is left on disk on failure, so a failed job is reproducible by
        # hand by rerunning the same command.
        if succeeded:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            log.warning("%s: job left at %s for reproduction", rec.name, workdir)


def substitute_key(template: Mapping[str, Any], key: Mapping[str, Scalar]) -> dict[str, Any]:
    """Resolve a declared input's key against the recipe's own key."""
    return {field: _substitute(value, key) for field, value in template.items()}


def _materialize(
    cache: Cache,
    ctx: Context,
    rec: Recipe,
    declared: ForeignInput,
    key: Mapping[str, Scalar],
    workdir: Path,
) -> Path:
    resolved_key = substitute_key(declared.key, key)
    data = ctx.get_bytes(declared.recipe, part=declared.part, **resolved_key)
    if not isinstance(data, bytes):
        raise ForeignRecipeError(
            f"{rec.name}: input {declared.name!r} resolves to a partitioned artifact with "
            f"{len(data)} parts. Name a part in the manifest; materializing a whole "
            f"partitioned value as one file is not supported. See DECISIONS.md D30."
        )
    path = workdir / declared.name
    path.write_bytes(data)
    return path


def _substitute(template: Any, key: Mapping[str, Scalar]) -> Any:
    """`"${name}"` substitutes the typed scalar; anything else interpolates to a string.

    The distinction matters because these feed `key_hash`, where `14` and `"14"` are
    different artifacts. See DECISIONS.md D13.
    """
    if not isinstance(template, str):
        return template
    referenced = _referenced_params(template)
    if not referenced:
        return template
    for name in referenced:
        if name not in key:
            raise ForeignRecipeError(f"${{{name}}} is not among this recipe's parameters")
    if template == f"${{{referenced[0]}}}" and len(referenced) == 1:
        return key[referenced[0]]
    out = template
    for name in referenced:
        out = out.replace(f"${{{name}}}", str(key[name]))
    return out


def _invoke(rec: Recipe, spec: ForeignSpec, job_file: Path, output: Path, policy: Policy) -> Any:
    from matview.codecs import get_codec
    from matview.errors import DeadlineExceeded

    command = [*spec.command, str(job_file)]
    remaining = policy.remaining()
    try:
        completed = subprocess.run(
            command,
            cwd=spec.workdir,
            capture_output=True,
            timeout=remaining,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ForeignRecipeError(f"{rec.name}: cannot run {command[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeadlineExceeded(rec.name, remaining or 0.0) from exc

    # stdout and stderr are logging only, so a stray print cannot corrupt a value.
    if completed.stdout:
        log.info("%s stdout: %s", rec.name, completed.stdout.decode("utf-8", "replace").rstrip())
    if completed.stderr:
        log.info("%s stderr: %s", rec.name, completed.stderr.decode("utf-8", "replace").rstrip())

    if completed.returncode != 0:
        raise ForeignRecipeError(
            f"{rec.name}: {' '.join(command)} exited {completed.returncode}. "
            f"stderr: {completed.stderr.decode('utf-8', 'replace').strip()[:2000]}"
        )
    if not output.exists():
        raise ForeignRecipeError(
            f"{rec.name}: exited 0 but wrote no output file. The result is read from the "
            f"output path, never from stdout."
        )

    # The output is decoded and then re-encoded canonically by the caller, so a foreign
    # process cannot break byte-determinism. See DECISIONS.md D20.
    try:
        return get_codec(rec.codec).decode(output.read_bytes())
    except Exception as exc:
        raise ForeignRecipeError(
            f"{rec.name}: output is not readable as codec {rec.codec!r}: {exc}"
        ) from exc
