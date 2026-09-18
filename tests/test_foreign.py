"""Stage 7: foreign recipes.

The "foreign" language here is Python run as a subprocess, which exercises exactly the
same contract an R or Julia script would meet: one JSON job file as the only argument,
the result read from the output path and never from stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from matview import Cache, Registry, SqliteStore, recipe
from matview.errors import DeadlineExceeded, ForeignRecipeError
from matview.foreign import load_manifest, load_manifests

SCRIPT = """
import json, sys
with open(sys.argv[1]) as handle: job = json.load(handle)
assert len(sys.argv) == 2, "the job file is the only argument"
print("chatter on stdout that must not reach the cache")
sys.stderr.write("chatter on stderr\\n")
{body}
"""


def write_recipe(tmp_path: Path, name: str, *, body: str, manifest: str) -> Path:
    directory = tmp_path / "recipes"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.py").write_text(SCRIPT.format(body=body), encoding="utf-8")
    (directory / f"{name}.toml").write_text(manifest, encoding="utf-8")
    return directory


def manifest_for(name: str, *, extra: str = "", params: str = "", inputs: str = "") -> str:
    return f'''
name = "{name}"
version = 1
codec = "json"
command = [{json.dumps(sys.executable)}, "{name}.py"]
source = "{name}.py"
{extra}

[params]
{params or 'dataset = { type = "string" }'}
{inputs}
'''


# --- the happy path --------------------------------------------------------


def test_a_manifest_registers_a_recipe_without_its_language_installed(
    tmp_path: Path, registry: Registry
) -> None:
    directory = write_recipe(
        tmp_path,
        "tree",
        body='open(job["output"], "w").write(json.dumps({"n": len(job["params"]["dataset"])}))',
        manifest=manifest_for("tree", extra='index = ["dataset"]\ndeterministic = false'),
    )
    (recipes,) = load_manifests(directory, registry=registry)
    assert recipes.name == "tree"
    assert recipes.version == 1
    assert recipes.deterministic is False
    assert recipes.index == ("dataset",)
    assert recipes.params == ("dataset",)
    assert recipes.source_hash is not None, "the single source file is hashed, advisory only"
    assert recipes.fn is None


def test_a_foreign_recipe_computes_and_is_cached(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    directory = write_recipe(
        tmp_path,
        "tree",
        body='open(job["output"], "w").write(json.dumps({"n": len(job["params"]["dataset"])}))',
        manifest=manifest_for("tree", extra='index = ["dataset"]'),
    )
    load_manifests(directory, registry=registry)

    assert cache.get("tree", dataset="ie-ielex") == {"n": 8}
    (ref,) = cache.query(recipe="tree")
    assert ref.valid is True
    assert cache.query(dataset="ie-ielex"), "the declared index field is promoted"

    # A second read is a cache hit: the subprocess does not run again.
    (directory / "tree.py").unlink()
    assert cache.get("tree", dataset="ie-ielex") == {"n": 8}


def test_stdout_cannot_corrupt_the_value(tmp_path: Path, cache: Cache, registry: Registry) -> None:
    directory = write_recipe(
        tmp_path,
        "noisy",
        body='print(json.dumps({"n": 999}));open(job["output"],"w").write(json.dumps({"n": 1}))',
        manifest=manifest_for("noisy"),
    )
    load_manifests(directory, registry=registry)
    assert cache.get("noisy", dataset="x") == {"n": 1}, "read from output, never from stdout"


def test_the_output_is_re_encoded_canonically(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    """A foreign process cannot break byte-determinism. See DECISIONS.md D20."""
    directory = write_recipe(
        tmp_path,
        "sloppy",
        body='open(job["output"], "w").write(\'{ "b" : 2,   "a" : 1 }\')',
        manifest=manifest_for("sloppy"),
    )
    load_manifests(directory, registry=registry)

    assert cache.get("sloppy", dataset="x") == {"a": 1, "b": 2}
    assert cache.get_bytes("sloppy", dataset="x") == b'{"a":1,"b":2}'


# --- failures are never cached ---------------------------------------------


@pytest.mark.parametrize(
    ("label", "body", "match"),
    [
        ("nonzero exit", "sys.exit(3)", "exited 3"),
        ("no output file", "pass", "wrote no output"),
        ("unparseable output", 'open(job["output"],"w").write("not json at all")', "not readable"),
    ],
)
def test_a_failed_foreign_compute_is_not_cached(
    tmp_path: Path, cache: Cache, registry: Registry, label: str, body: str, match: str
) -> None:
    directory = write_recipe(tmp_path, "broken", body=body, manifest=manifest_for("broken"))
    load_manifests(directory, registry=registry)

    with pytest.raises(ForeignRecipeError, match=match):
        cache.get("broken", dataset="x")

    assert cache.query(recipe="broken") == [], label


def test_the_job_file_survives_a_failure_for_reproduction(
    tmp_path: Path, cache: Cache, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    directory = write_recipe(
        tmp_path, "broken", body="sys.exit(1)", manifest=manifest_for("broken")
    )
    load_manifests(directory, registry=registry)

    with caplog.at_level("WARNING", logger="matview.foreign"), pytest.raises(ForeignRecipeError):
        cache.get("broken", dataset="x")

    left = [Path(str(rec.args[1])) for rec in caplog.records if rec.args and len(rec.args) == 2]
    assert left, "the location of the failed job is reported"
    job = json.loads((left[0] / "job.json").read_text())
    assert set(job) == {"params", "inputs", "output", "deadline_s"}
    assert job["params"] == {"dataset": "x"}


def test_a_missing_interpreter_is_a_clear_failure(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    directory = tmp_path / "recipes"
    directory.mkdir()
    (directory / "ghost.py").write_text("", encoding="utf-8")
    (directory / "ghost.toml").write_text(
        '\nname = "ghost"\nversion = 1\ncodec = "json"\n'
        'command = ["definitely-not-an-interpreter", "ghost.py"]\nsource = "ghost.py"\n'
        '\n[params]\ndataset = { type = "string" }\n',
        encoding="utf-8",
    )
    load_manifests(directory, registry=registry)

    with pytest.raises(ForeignRecipeError, match="cannot run"):
        cache.get("ghost", dataset="x")


def test_the_deadline_kills_the_process(tmp_path: Path, cache: Cache, registry: Registry) -> None:
    directory = write_recipe(
        tmp_path, "slow", body="import time; time.sleep(30)", manifest=manifest_for("slow")
    )
    load_manifests(directory, registry=registry)

    with pytest.raises(DeadlineExceeded):
        cache.get("slow", dataset="x", deadline=0.5)
    assert cache.query(recipe="slow") == []


# --- declared inputs -------------------------------------------------------


def test_declared_inputs_are_materialized_as_files(
    tmp_path: Path, cache: Cache, registry: Registry, store: SqliteStore
) -> None:
    @recipe(version=1, codec="json", index=("dataset",), registry=registry)
    def distance_matrix(ctx, *, dataset: str):
        return {"rows": [[0, 1], [1, 0]], "dataset": dataset}

    directory = write_recipe(
        tmp_path,
        "phylo_tree",
        body=(
            'matrix = json.loads(open(job["inputs"]["matrix"]).read());'
            'open(job["output"],"w").write(json.dumps('
            '{"n": len(matrix["rows"]), "model": job["params"]["model"]}))'
        ),
        manifest=manifest_for(
            "phylo_tree",
            params='dataset = { type = "string" }\nmodel = { type = "string", default = "GTR" }',
            inputs=(
                '\n[[inputs]]\nname = "matrix"\nrecipe = "distance_matrix"\n'
                'key = { dataset = "${dataset}" }\n'
            ),
        ),
    )
    load_manifests(directory, registry=registry)

    assert cache.get("phylo_tree", dataset="ie-ielex") == {"n": 2, "model": "GTR"}

    (ref,) = cache.query(recipe="phylo_tree")
    edges = store.deps_of(ref.id)
    assert [edge.recipe for edge in edges] == ["distance_matrix"], "the edge was recorded"

    # And the cascade reaches it like any other dependency.
    cache.store.invalidate([cache.query(recipe="distance_matrix")[0].id])
    assert cache.query(recipe="phylo_tree")[0].valid is False


def test_a_substituted_scalar_keeps_its_type(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    """`"${window}"` must yield the integer 14, not the string "14".

    These feed key_hash, where the two are different artifacts. DECISIONS.md D13.
    """
    seen: list[object] = []

    @recipe(version=1, codec="json", registry=registry)
    def upstream(ctx, *, window: int, label: str = "plain"):
        seen.append(window)
        return {"w": window, "label": label}

    directory = write_recipe(
        tmp_path,
        "typed",
        body=(
            'up = json.loads(open(job["inputs"]["up"]).read());'
            'open(job["output"],"w").write(json.dumps(up))'
        ),
        manifest=manifest_for(
            "typed",
            params='window = { type = "int" }',
            inputs=(
                '\n[[inputs]]\nname = "up"\nrecipe = "upstream"\n'
                'key = { window = "${window}", label = "w-${window}" }\n'
            ),
        ),
    )
    load_manifests(directory, registry=registry)

    assert cache.get("typed", window=14) == {"w": 14, "label": "w-14"}
    assert seen == [14]
    assert not isinstance(seen[0], str), "a bare ${name} substitutes the typed scalar"


def test_a_partitioned_input_must_name_a_part(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    from matview.codecs import register_codec
    from tests.test_scheduler import YearCodec

    register_codec(YearCodec())

    @recipe(version=1, codec="test-years", registry=registry)
    def series(ctx, *, dataset: str):
        return {"2024": [1], "2025": [2]}

    directory = write_recipe(
        tmp_path,
        "whole",
        body='open(job["output"],"w").write("{}")',
        manifest=manifest_for(
            "whole",
            inputs=(
                '\n[[inputs]]\nname = "s"\nrecipe = "series"\nkey = { dataset = "${dataset}" }\n'
            ),
        ),
    )
    load_manifests(directory, registry=registry)

    with pytest.raises(ForeignRecipeError, match="Name a part"):
        cache.get("whole", dataset="x")


# --- manifests are validated before anything runs --------------------------


def test_a_declared_cycle_is_caught_at_load_time(tmp_path: Path, registry: Registry) -> None:
    """The privileged case: a declared graph is cycle-checked before it executes."""
    directory = tmp_path / "recipes"
    directory.mkdir()
    for name, other in (("a", "b"), ("b", "a")):
        (directory / f"{name}.toml").write_text(
            f'\nname = "{name}"\nversion = 1\ncodec = "json"\ncommand = ["true"]\n'
            f'\n[params]\ndataset = {{ type = "string" }}\n'
            f'\n[[inputs]]\nname = "x"\nrecipe = "{other}"\nkey = {{ dataset = "${{dataset}}" }}\n',
            encoding="utf-8",
        )
    with pytest.raises(ForeignRecipeError, match="declared dependency cycle"):
        load_manifests(directory, registry=registry)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ('name = "x"\nversion = 1\ncommand = ["true"]\n', "missing required field 'codec'"),
        ('name = "x"\nversion = 1\ncodec = "json"\n', "missing required field 'command'"),
        ('name = "x"\nversion = 1\ncodec = "json"\ncommand = []\n', "non-empty list"),
        (
            'name = "x"\nversion = 1\ncodec = "json"\ncommand = ["true"]\n'
            '[params]\nn = { type = "complex" }\n',
            "unknown type",
        ),
        (
            'name = "x"\nversion = 1\ncodec = "json"\ncommand = ["true"]\n'
            '[params]\nn = { type = "int", default = "no" }\n',
            "declares int",
        ),
        (
            'name = "x"\nversion = 1\ncodec = "json"\ncommand = ["true"]\n'
            '[params]\nn = { type = "int" }\n'
            '[[inputs]]\nname = "i"\nrecipe = "r"\nkey = { d = "${nope}" }\n',
            "not a parameter",
        ),
    ],
)
def test_a_bad_manifest_is_rejected_with_its_path(
    tmp_path: Path, registry: Registry, bad: str, match: str
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ForeignRecipeError, match=match):
        load_manifest(path, registry=registry)


def test_a_changed_source_without_a_version_bump_warns(
    tmp_path: Path, cache: Cache, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    directory = write_recipe(
        tmp_path,
        "drift",
        body='open(job["output"],"w").write(json.dumps({"n": 1}))',
        manifest=manifest_for("drift"),
    )
    load_manifests(directory, registry=registry)
    cache.get("drift", dataset="x")

    (directory / "drift.py").write_text(
        SCRIPT.format(body='open(job["output"],"w").write(json.dumps({"n": 2}))'), encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="matview.foreign"):
        load_manifests(directory, registry=registry, store=cache.store)

    assert any("source changed but the version did not" in rec.message for rec in caplog.records)
    assert cache.get("drift", dataset="x") == {"n": 1}, (
        "the hash is advisory and never fingerprinted, so it invalidates nothing"
    )


def test_plan_walks_through_a_foreign_node_without_running_it(
    tmp_path: Path, cache: Cache, registry: Registry
) -> None:
    """The declared graph is the privileged case, and this is the concrete payoff.

    A Python recipe's dependencies are only knowable by running it, so a plan stops at
    an absent one. A manifest says what the inputs are, so the plan sees the whole
    subgraph on a completely cold cache.
    """
    ran = {"n": 0}

    @recipe(version=1, codec="json", index=("dataset",), registry=registry)
    def distance_matrix(ctx, *, dataset: str):
        ran["n"] += 1
        return {"rows": [[0, 1], [1, 0]]}

    directory = write_recipe(
        tmp_path,
        "tree",
        body='open(job["output"],"w").write("{}")',
        manifest=manifest_for(
            "tree",
            inputs=(
                '\n[[inputs]]\nname = "matrix"\nrecipe = "distance_matrix"\n'
                'key = { dataset = "${dataset}" }\n'
            ),
        ),
    )
    load_manifests(directory, registry=registry)

    report = cache.plan("tree", dataset="ie-ielex")
    assert sorted(node.recipe for node in report.absent) == ["distance_matrix", "tree"]
    assert ran["n"] == 0, "planning runs nothing, Python or foreign"
    assert cache.query() == []
