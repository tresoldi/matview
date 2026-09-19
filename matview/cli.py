"""`matview`, a read-only inspection tool for a cache database.

The commands divide by what they need, and the division is the whole design:

- **Metadata** — `info`, `ls`, `show`, `stats`, `verify` — works against any matview
  database with nothing imported. Names, versions, digests, sizes, timestamps, validity
  and dependency edges are all recorded in the file.
- **Key lookup by parameters** needs `--recipes`, and only for one reason: declared
  defaults are filled in *before* a key is hashed, so `show sma symbol=AAPL` cannot know
  the right `key_hash` unless something tells it what `interval` defaults to.
- **`plan`** needs `--recipes`, because a plan resolves recipes.

Decoding a value needs neither. The codec is recorded on the recipe row, so `cat --id`
decodes without importing anything — only a codec the *application* registered itself
needs `--recipes`. See DECISIONS.md D37.

Nothing here writes. `drop` and `gc` exist on `Cache`; they are deliberately not exposed
yet.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matview.errors import MatviewError
from matview.fingerprint import digest_bytes
from matview.store.sqlite import SqliteStore, inspect_database
from matview.types import ArtifactRef

__all__ = ["main"]

PROGRAM = "matview"

#: Exit statuses. 2 is argparse's own for a usage error.
OK = 0
PROBLEM = 1


# --- helpers ---------------------------------------------------------------


def _iso(value: int | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover - unreachable, the loop returns first


def _parse_value(raw: str) -> Any:
    """Parse a command-line key field as JSON, falling back to the literal string.

    So `window=200` is the integer 200, `flag=true` is a boolean and `x=null` is null —
    which matters, because `200` and `"200"` canonicalize to different keys and therefore
    different artifacts. A value that is not valid JSON is taken as a string, and a string
    that looks like a number can be forced with JSON quoting: `symbol='"200"'`.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_pairs(pairs: Sequence[str], *, what: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name:
            raise MatviewError(f"{what} must be given as field=value, got {pair!r}")
        out[name] = _parse_value(raw)
    return out


def _open(path: str) -> SqliteStore:
    """Open the database read-only.

    Not a precaution but a correctness requirement: a read-write open would create a
    database on a mistyped path and then report it as empty, which is the opposite of
    what an inspection tool is for.
    """
    try:
        return SqliteStore(path, read_only=True)
    except MatviewError as exc:
        if not Path(path).exists():
            raise
        # The file is there but this build will not read it, which is the one case where
        # `info` helps: it is version-blind. Suggesting it for a missing file would send
        # the reader to a command that fails in the same way.
        raise MatviewError(
            f"{exc}\n\nTry `{PROGRAM} info --db {path}`, which reads any version."
        ) from exc


def _load_recipes(specs: Sequence[str]) -> None:
    """Import the modules that register the application's recipes.

    Accepts a dotted module path or a path to a .py file, because a consumer's recipes
    are as likely to live in a script as in an installed package.
    """
    for spec in specs:
        if spec.endswith(".py") or os.sep in spec:
            path = Path(spec).resolve()
            if not path.exists():
                raise MatviewError(f"--recipes {spec}: no such file")
            # Registered under a prefixed name, not the file's bare stem: loading
            # `recipes.py` must not shadow an installed `recipes` package for the rest of
            # the process, and a recipe module's own imports may well need the real one.
            name = f"_matview_recipes_{path.stem}"
            module_spec = importlib.util.spec_from_file_location(name, path)
            if module_spec is None or module_spec.loader is None:  # pragma: no cover
                raise MatviewError(f"--recipes {spec}: cannot be imported as a module")
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[name] = module
            module_spec.loader.exec_module(module)
        else:
            try:
                importlib.import_module(spec)
            except ImportError as exc:
                raise MatviewError(f"--recipes {spec}: {exc}") from exc


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str], out: Any) -> None:
    if not rows:
        print("(nothing)", file=out)
        return
    widths = {c: max(len(c), *(len(str(row.get(c, ""))) for row in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns), file=out)
    print("  ".join("-" * widths[c] for c in columns), file=out)
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns), file=out)


def _emit(payload: Any, *, as_json: bool, out: Any, render: Callable[[], None]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=out)
    else:
        render()


def _ref_dict(ref: ArtifactRef) -> dict[str, Any]:
    return {
        "id": ref.id,
        "recipe": ref.recipe,
        "version": ref.version,
        "part": ref.part,
        "valid": ref.valid,
        "key_hash": ref.key_hash,
        "key": json.loads(ref.key_json),
        "digest": ref.digest,
        "fingerprint": ref.fingerprint,
        "n_bytes": ref.n_bytes,
        "computed_at": ref.computed_at,
        "refreshed_at": ref.refreshed_at,
        "compute_ms": ref.compute_ms,
    }


def _resolve_target(store: SqliteStore, args: argparse.Namespace) -> list[ArtifactRef]:
    """Find the artifacts a `show`/`cat` invocation names, or explain why it found none."""
    if args.id is not None:
        ref = store.ref_by_id(args.id)
        if ref is None:
            raise MatviewError(f"no artifact with id {args.id}")
        return [ref]

    if args.key_hash is not None:
        refs = store.refs_by_key_hash(args.key_hash, args.recipe)
        if not refs:
            raise MatviewError(f"no artifact with key hash {args.key_hash}")
        return refs

    if args.recipe is None:
        raise MatviewError("give a recipe name, or --id, or --key-hash")

    from matview.fingerprint import key_hash as hash_key

    key = _parse_pairs(args.key, what="a key field")
    if args.recipes:
        from matview.registry import REGISTRY

        key = REGISTRY.get(args.recipe).bind(key)
    refs = store.refs_by_key_hash(hash_key(key), args.recipe)
    if refs:
        return refs

    stored = store.list_refs(args.recipe, limit=1)
    if stored and not args.recipes:
        raise MatviewError(
            f"no artifact of {args.recipe} matches that key, but {args.recipe} does have "
            f"stored artifacts.\n\nA key is hashed *after* the recipe's declared defaults "
            f"are filled in, so a partial key cannot match. Either pass every field, or "
            f"pass --recipes MODULE so the defaults can be applied.\n\nTry "
            f"`{PROGRAM} ls --recipe {args.recipe} --db {args.db}` to see the stored keys."
        )
    raise MatviewError(f"no artifact of {args.recipe} matches that key")


# --- commands --------------------------------------------------------------


def cmd_info(args: argparse.Namespace, out: Any) -> int:
    """Describe the database, whatever version wrote it."""
    info = inspect_database(args.db)

    def render() -> None:
        print(
            f"{info['path']}  ({_human_bytes(info['n_bytes'])}, {info['journal_mode']})", file=out
        )
        print(
            f"  schema_version            {info['schema_version']} "
            f"(this build implements {info['implemented_schema_version']})",
            file=out,
        )
        print(
            f"  fingerprint_spec_version  {info['fingerprint_spec_version']} "
            f"(this build implements {info['implemented_fingerprint_spec_version']})",
            file=out,
        )
        print(f"  readable by this build    {'yes' if info['readable'] else 'NO'}", file=out)
        print("\n  rows", file=out)
        for table in info["tables"]:
            print(f"    {table:<14} {info['row_counts'][table]}", file=out)
        if not info["readable"]:
            print(
                "\n  Other commands will refuse this file: its hashes or its columns are "
                "not the ones\n  this build knows about. Nothing here has modified it.",
                file=out,
            )

    _emit(info, as_json=args.json, out=out, render=render)
    return OK if info["readable"] else PROBLEM


def cmd_ls(args: argparse.Namespace, out: Any) -> int:
    """List artifacts."""
    store = _open(args.db)
    try:
        valid = None if args.valid == args.invalid else args.valid
        refs = store.list_refs(
            args.recipe,
            version=args.version,
            part=args.part,
            valid=valid,
            fields={k: str(v) for k, v in _parse_pairs(args.where, what="--where").items()},
            limit=args.limit,
        )
        payload = [_ref_dict(ref) for ref in refs]

        def render() -> None:
            _table(
                [
                    {
                        "id": ref.id,
                        "recipe": ref.recipe,
                        "v": ref.version,
                        "part": "-" if ref.part is None else ref.part,
                        "valid": "yes" if ref.valid else "STALE",
                        "size": _human_bytes(ref.n_bytes),
                        "computed": _iso(ref.computed_at),
                        "key": ref.key_json,
                    }
                    for ref in refs
                ],
                ["id", "recipe", "v", "part", "valid", "size", "computed", "key"],
                out,
            )
            if args.limit is not None and len(refs) == args.limit:
                print(f"\n(stopped at --limit {args.limit})", file=out)

        _emit(payload, as_json=args.json, out=out, render=render)
        return OK
    finally:
        store.close()


def cmd_show(args: argparse.Namespace, out: Any) -> int:
    """Show one artifact in full, with its dependencies and dependents."""
    if args.recipes:
        _load_recipes(args.recipes)
    store = _open(args.db)
    try:
        refs = _resolve_target(store, args)
        if args.part is not None:
            refs = [ref for ref in refs if ref.part == args.part]
            if not refs:
                raise MatviewError(f"no part {args.part!r} among those artifacts")

        payload = []
        for ref in refs:
            deps = store.deps_of(ref.id)
            entry = _ref_dict(ref)
            entry["dependencies"] = [
                {
                    "recipe": edge.recipe,
                    "version": edge.version,
                    "part": edge.part,
                    "key": json.loads(edge.key_json) if edge.key_json else {},
                    "digest_at_compute_time": edge.digest,
                    "artifact_id": edge.artifact_id,
                }
                for edge in deps
            ]
            entry["dependents"] = store.dependents_of([ref.id])
            payload.append(entry)

        def render() -> None:
            for entry in payload:
                print(
                    f"artifact {entry['id']}: {entry['recipe']} v{entry['version']}"
                    + ("" if entry["part"] is None else f" part={entry['part']!r}"),
                    file=out,
                )
                print(f"  key           {json.dumps(entry['key'], sort_keys=True)}", file=out)
                print(f"  key_hash      {entry['key_hash']}", file=out)
                print(f"  digest        {entry['digest']}", file=out)
                print(f"  fingerprint   {entry['fingerprint']}", file=out)
                print(f"  valid         {'yes' if entry['valid'] else 'no (stale)'}", file=out)
                print(f"  size          {_human_bytes(entry['n_bytes'])}", file=out)
                print(f"  computed_at   {_iso(entry['computed_at'])}", file=out)
                print(f"  refreshed_at  {_iso(entry['refreshed_at'])}", file=out)
                print(f"  compute_ms    {entry['compute_ms']}", file=out)
                if entry["dependencies"]:
                    print("  dependencies", file=out)
                    for dep in entry["dependencies"]:
                        part = "" if dep["part"] is None else f" part={dep['part']!r}"
                        print(
                            f"    {dep['recipe']} v{dep['version']}{part} "
                            f"{json.dumps(dep['key'], sort_keys=True)} "
                            f"(id {dep['artifact_id']})",
                            file=out,
                        )
                else:
                    print("  dependencies  (none: this is a root)", file=out)
                print(
                    f"  dependents    {entry['dependents'] or '(none)'}",
                    file=out,
                )

        _emit(
            payload if len(payload) > 1 else payload[0], as_json=args.json, out=out, render=render
        )
        return OK
    finally:
        store.close()


def cmd_stats(args: argparse.Namespace, out: Any) -> int:
    """Summarize the database."""
    store = _open(args.db)
    try:
        summary = store.summary()
        recipes = store.recipe_rows()
        payload = {**summary, "by_recipe": recipes}

        def render() -> None:
            print(f"{summary['path']}", file=out)
            print(
                f"  schema {summary['schema_version']}, "
                f"fingerprint spec {summary['fingerprint_spec_version']}",
                file=out,
            )
            print(
                f"  {summary['artifacts']} artifacts "
                f"({summary['invalid']} stale), {_human_bytes(summary['n_bytes'])}, "
                f"largest {_human_bytes(summary['largest_artifact_bytes'])}",
                file=out,
            )
            print(
                f"  {summary['recipes']} recipes in "
                f"{summary['recipe_versions']} versions, "
                f"{summary['dep_edges']} dependency edges, "
                f"{summary['pending_tasks']} pending tasks",
                file=out,
            )
            print(
                f"  computed between {_iso(summary['oldest_computed_at'])} and "
                f"{_iso(summary['newest_computed_at'])}, "
                f"{summary['compute_ms']} ms total",
                file=out,
            )
            print(file=out)
            _table(
                [
                    {
                        "recipe": row["name"],
                        "v": row["version"],
                        "codec": row["codec"],
                        "artifacts": row["artifacts"],
                        "stale": row["invalid"],
                        "size": _human_bytes(row["n_bytes"]),
                    }
                    for row in recipes
                ],
                ["recipe", "v", "codec", "artifacts", "stale", "size"],
                out,
            )

        _emit(payload, as_json=args.json, out=out, render=render)
        return OK
    finally:
        store.close()


def cmd_verify(args: argparse.Namespace, out: Any) -> int:
    """Recompute every stored payload's digest and compare it to the recorded one.

    This is the one corruption the library cannot detect by itself. A digest that no
    longer matches its bytes does not error anywhere: it silently invalidates every
    fingerprint above it, and the cache goes on looking healthy.
    """
    store = _open(args.db)
    try:
        checked = 0
        mismatches: list[dict[str, Any]] = []
        for artifact_id, recorded, data in store.iter_payloads(args.recipe):
            checked += 1
            actual = digest_bytes(data)
            if actual != recorded:
                ref = store.ref_by_id(artifact_id)
                mismatches.append(
                    {
                        "id": artifact_id,
                        "recipe": None if ref is None else ref.recipe,
                        "version": None if ref is None else ref.version,
                        "part": None if ref is None else ref.part,
                        "key": None if ref is None else json.loads(ref.key_json),
                        "recorded_digest": recorded,
                        "actual_digest": actual,
                    }
                )
            if args.limit is not None and checked >= args.limit:
                break

        payload = {"checked": checked, "mismatches": mismatches}

        def render() -> None:
            print(f"checked {checked} artifact(s)", file=out)
            if not mismatches:
                print("all digests match their payloads", file=out)
                return
            print(f"\n{len(mismatches)} MISMATCH(ES):", file=out)
            for bad in mismatches:
                print(
                    f"  artifact {bad['id']} ({bad['recipe']} v{bad['version']}): "
                    f"recorded {bad['recorded_digest'][:16]}…, "
                    f"actual {bad['actual_digest'][:16]}…",
                    file=out,
                )

        _emit(payload, as_json=args.json, out=out, render=render)
        return PROBLEM if mismatches else OK
    finally:
        store.close()


def cmd_plan(args: argparse.Namespace, out: Any) -> int:
    """Walk the graph without computing anything."""
    _load_recipes(args.recipes)
    from matview.cache import Cache
    from matview.registry import REGISTRY

    store = _open(args.db)
    try:
        cache = Cache(store=store, registry=REGISTRY)
        report = cache.plan(
            args.recipe,
            part=args.part,
            on_miss=args.on_miss,
            force_deps=args.force_deps,
            **_parse_pairs(args.key, what="a key field"),
        )
        payload = {
            "current": report.is_current,
            "absent": [
                {
                    "recipe": node.recipe,
                    "version": node.version,
                    "key": dict(node.key),
                    "part": node.part,
                    "action": node.action,
                    "path": list(node.path),
                }
                for node in report.absent
            ],
            "stale": [_ref_dict(ref) for ref in report.stale],
            "valid": [_ref_dict(ref) for ref in report.valid],
            "nondeterministic": list(report.nondeterministic),
        }
        _emit(
            payload, as_json=args.json, out=out, render=lambda: print(report.describe(), file=out)
        )
        return OK if report.is_current else PROBLEM
    finally:
        store.close()


def cmd_cat(args: argparse.Namespace, out: Any) -> int:
    """Print an artifact's value, or its raw bytes.

    Decoding needs no `--recipes`: the codec is recorded on the recipe row. Only a codec
    the application registered itself does.
    """
    if args.recipes:
        _load_recipes(args.recipes)
    store = _open(args.db)
    try:
        refs = _resolve_target(store, args)
        if args.part is not None:
            refs = [ref for ref in refs if ref.part == args.part]
        if len(refs) != 1:
            raise MatviewError(
                f"that names {len(refs)} artifacts; add --part, or use --id. "
                f"Parts present: {', '.join(str(r.part) for r in refs)}"
            )
        artifact = store.fetch_by_id(refs[0].id)
        assert artifact is not None

        if args.raw:
            buffer = getattr(out, "buffer", None)
            if buffer is None:
                out.write(artifact.data.decode("utf-8", "replace"))
            else:
                buffer.write(artifact.data)
            return OK

        from matview.codecs import CodecError, get_codec

        try:
            codec = get_codec(artifact.codec)
        except CodecError as exc:
            raise MatviewError(
                f"{exc}\n\nThe recipe row records codec {artifact.codec!r}. If your "
                f"application defines it, pass --recipes MODULE so it is registered; "
                f"otherwise use --raw."
            ) from exc
        value = codec.decode(artifact.data)
        if args.json:
            print(json.dumps(value, indent=2, sort_keys=True, default=str), file=out)
        else:
            print(value, file=out)
        return OK
    finally:
        store.close()


# --- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Inspect a matview cache database. Nothing here writes to it.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("MATVIEW_DB"),
        help="path to the cache database (default: $MATVIEW_DB)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output")

    recipes = argparse.ArgumentParser(add_help=False)
    recipes.add_argument(
        "--recipes",
        action="append",
        default=[],
        metavar="MODULE",
        help="import this module or .py file so its recipes are registered; repeatable",
    )

    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("recipe", nargs="?", help="recipe name")
    target.add_argument("key", nargs="*", metavar="field=value", help="key fields, parsed as JSON")
    target.add_argument("--id", type=int, help="address the artifact by row id instead")
    target.add_argument("--key-hash", help="address the artifact by key hash instead")
    target.add_argument("--part", help="select one part")

    info = subparsers.add_parser(
        "info", parents=[common], help="describe the database, whatever version wrote it"
    )
    info.set_defaults(func=cmd_info)

    ls = subparsers.add_parser("ls", parents=[common], help="list artifacts")
    ls.add_argument("--recipe", help="only this recipe")
    ls.add_argument("--version", type=int, help="only this recipe version")
    ls.add_argument("--part", help="only this part")
    ls.add_argument("--valid", action="store_true", help="only current artifacts")
    ls.add_argument("--invalid", action="store_true", help="only stale artifacts")
    ls.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="filter on a promoted index field; repeatable",
    )
    ls.add_argument("--limit", type=int, help="stop after this many")
    ls.set_defaults(func=cmd_ls)

    show = subparsers.add_parser(
        "show", parents=[common, recipes, target], help="show one artifact in full"
    )
    show.set_defaults(func=cmd_show)

    stats = subparsers.add_parser("stats", parents=[common], help="summarize the database")
    stats.set_defaults(func=cmd_stats)

    verify = subparsers.add_parser(
        "verify", parents=[common], help="check every payload against its recorded digest"
    )
    verify.add_argument("--recipe", help="only this recipe")
    verify.add_argument("--limit", type=int, help="stop after this many")
    verify.set_defaults(func=cmd_verify)

    plan = subparsers.add_parser(
        "plan", parents=[common, recipes], help="walk the graph without computing (needs --recipes)"
    )
    plan.add_argument("recipe", help="recipe name")
    plan.add_argument("key", nargs="*", metavar="field=value", help="key fields, parsed as JSON")
    plan.add_argument("--part", help="select one part")
    plan.add_argument("--on-miss", choices=("compute", "enqueue", "raise"), default="compute")
    plan.add_argument("--force-deps", action="store_true")
    plan.set_defaults(func=cmd_plan)

    cat = subparsers.add_parser(
        "cat", parents=[common, recipes, target], help="print an artifact's decoded value"
    )
    cat.add_argument("--raw", action="store_true", help="write the stored bytes instead")
    cat.set_defaults(func=cmd_cat)

    return parser


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = sys.stdout if out is None else out

    if args.command == "plan" and not args.recipes:
        parser.error(
            "plan needs --recipes MODULE: a plan resolves recipes, so they must be registered"
        )
    if not args.db:
        parser.error("no database given: pass --db PATH or set MATVIEW_DB")

    try:
        return int(args.func(args, stream))
    except MatviewError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return PROBLEM
    except BrokenPipeError:  # pragma: no cover - `matview ls | head`
        return OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
