"""The `matview` inspection CLI.

The division under test is the one that makes the tool useful: metadata against any
database with nothing imported, and `--recipes` required only where it genuinely is.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from matview import Cache, Registry, SqliteStore, recipe
from matview.cli import main
from matview.registry import REGISTRY

RECIPES_MODULE = """
from matview import recipe

@recipe(version=1, codec="json", index=("symbol",), on_miss="raise")
def ohlcv(ctx, *, symbol: str, interval: str = "1d"):
    return {"close": [1.0, 2.0, 3.0], "symbol": symbol}

@recipe(version=3, codec="json", index=("symbol",))
def sma(ctx, *, symbol: str, window: int = 2, interval: str = "1d"):
    close = ctx.get("ohlcv", symbol=symbol, interval=interval)["close"]
    return {"sma": sum(close[-window:]) / window}
"""


@pytest.fixture(autouse=True)
def _clean_global_registry() -> Iterator[None]:
    """`--recipes` imports into the global registry, so it has to be put back."""
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture
def recipes_file(tmp_path: Path) -> Path:
    path = tmp_path / "demo_recipes.py"
    path.write_text(RECIPES_MODULE, encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path, registry: Registry) -> Path:
    """A small cache with two recipes, two symbols, one dependency and one stale row."""
    path = tmp_path / "cache.db"

    @recipe(version=1, codec="json", index=("symbol",), on_miss="raise", registry=registry)
    def ohlcv(ctx, *, symbol: str, interval: str = "1d"):
        return {"close": [1.0, 2.0, 3.0], "symbol": symbol}

    @recipe(version=3, codec="json", index=("symbol",), registry=registry)
    def sma(ctx, *, symbol: str, window: int = 2, interval: str = "1d"):
        close = ctx.get("ohlcv", symbol=symbol, interval=interval)["close"]
        return {"sma": sum(close[-window:]) / window}

    store = SqliteStore(path)
    cache = Cache(store=store, registry=registry)
    for symbol in ("AAPL", "MSFT"):
        cache.get("ohlcv", symbol=symbol, on_miss="compute")
        cache.get("sma", symbol=symbol)
    store.invalidate([ref.id for ref in cache.query(recipe="sma") if "MSFT" in ref.key_json])
    store.close()
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def run_json(*argv: str) -> Any:
    code, text = run(*argv)
    assert code in (0, 1), text
    return json.loads(text)


# --- info ------------------------------------------------------------------


def test_info_describes_a_readable_database(db: Path) -> None:
    code, text = run("info", "--db", str(db))
    assert code == 0
    assert "readable by this build    yes" in text
    assert "artifact       4" in text


def test_info_reads_a_database_this_build_refuses(db: Path, capsys: pytest.CaptureFixture) -> None:
    """The whole point: a diagnostic tool is wanted when something is already wrong."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET v = '99' WHERE k = 'schema_version'")
    conn.commit()
    conn.close()

    code, text = run("info", "--db", str(db))
    assert code == 1, "it reports a problem"
    assert "schema_version            99" in text
    assert "readable by this build    NO" in text
    assert "Other commands will refuse" in text

    # And every other command does refuse, pointing at info.
    assert run("ls", "--db", str(db))[0] == 1
    assert "info --db" in capsys.readouterr().err


def test_info_on_a_missing_file_says_so(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("info", "--db", str(tmp_path / "nope.db"))[0] == 1
    assert "does not exist" in capsys.readouterr().err


# --- read-only -------------------------------------------------------------


def test_the_cli_never_creates_a_database(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A read-write open would create a database on a mistyped path and call it empty."""
    missing = tmp_path / "typo.db"
    assert run("ls", "--db", str(missing))[0] == 1
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists(), "inspecting a path must not bring it into being"


def test_a_read_only_store_refuses_to_write(db: Path) -> None:
    store = SqliteStore(db, read_only=True)
    try:
        assert store.summary()["artifacts"] == 4
        with pytest.raises(Exception, match="read-only"):
            store.invalidate([1])
    finally:
        store.close()


def test_a_read_only_open_does_not_change_the_journal_mode(tmp_path: Path) -> None:
    """Switching someone's journal mode while they are diagnosing is not a read."""
    path = tmp_path / "plain.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '2'), ('fingerprint_spec_version', '1');"
    )
    conn.commit()
    before = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert before != "wal"

    store = SqliteStore(path, read_only=True)
    store.close()

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == before
    conn.close()


# --- ls --------------------------------------------------------------------


def test_ls_lists_everything_with_no_recipes_imported(db: Path) -> None:
    code, text = run("ls", "--db", str(db))
    assert code == 0
    assert text.count("ohlcv") == 2
    assert text.count("sma") == 2
    assert "STALE" in text
    assert len(REGISTRY) == 0, "metadata inspection imports nothing"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("--recipe", "sma"), 2),
        (("--recipe", "ohlcv", "--valid"), 2),
        (("--invalid",), 1),
        (("--where", "symbol=AAPL"), 2),
        (("--where", "symbol=NVDA"), 0),
        (("--limit", "1"), 1),
        (("--version", "3"), 2),
    ],
)
def test_ls_filters(db: Path, argv: tuple[str, ...], expected: int) -> None:
    rows = run_json("ls", "--db", str(db), "--json", *argv)
    assert len(rows) == expected


def test_ls_json_carries_every_field_a_script_needs(db: Path) -> None:
    (row, *_) = run_json("ls", "--db", str(db), "--json", "--recipe", "ohlcv")
    assert set(row) == {
        "compute_ms",
        "computed_at",
        "digest",
        "fingerprint",
        "id",
        "key",
        "key_hash",
        "n_bytes",
        "part",
        "recipe",
        "refreshed_at",
        "valid",
        "version",
    }
    assert row["key"] == {"symbol": "AAPL", "interval": "1d"}


# --- show ------------------------------------------------------------------


def test_show_by_id_includes_dependencies_and_dependents(db: Path) -> None:
    (sma_row,) = run_json(
        "ls", "--db", str(db), "--json", "--recipe", "sma", "--where", "symbol=AAPL"
    )
    entry = run_json("show", "--db", str(db), "--json", "--id", str(sma_row["id"]))

    assert entry["recipe"] == "sma"
    assert [dep["recipe"] for dep in entry["dependencies"]] == ["ohlcv"]
    assert entry["dependencies"][0]["key"] == {"symbol": "AAPL", "interval": "1d"}
    assert entry["dependents"] == []

    root = run_json(
        "show", "--db", str(db), "--json", "--id", str(entry["dependencies"][0]["artifact_id"])
    )
    assert root["dependencies"] == []
    assert root["dependents"] == [sma_row["id"]]


def test_show_by_key_hash(db: Path) -> None:
    (row, *_) = run_json("ls", "--db", str(db), "--json", "--recipe", "ohlcv")
    entry = run_json("show", "--db", str(db), "--json", "--key-hash", row["key_hash"])
    assert entry["id"] == row["id"]


def test_a_partial_key_without_recipes_explains_itself(
    db: Path, capsys: pytest.CaptureFixture
) -> None:
    """The one thing that genuinely cannot work without the registry, said plainly."""
    assert run("show", "--db", str(db), "sma", "symbol=AAPL")[0] == 1
    message = capsys.readouterr().err
    assert "defaults are filled in" in message
    assert "--recipes MODULE" in message
    assert "matview ls --recipe sma" in message


def test_a_partial_key_with_recipes_resolves(db: Path, recipes_file: Path) -> None:
    entry = run_json(
        "show", "--db", str(db), "--json", "--recipes", str(recipes_file), "sma", "symbol=AAPL"
    )
    assert entry["key"] == {"symbol": "AAPL", "interval": "1d", "window": 2}


def test_a_complete_key_needs_no_recipes(db: Path) -> None:
    entry = run_json(
        "show", "--db", str(db), "--json", "sma", "symbol=AAPL", "interval=1d", "window=2"
    )
    assert entry["recipe"] == "sma"
    assert len(REGISTRY) == 0


def test_key_values_are_parsed_as_json_so_types_survive(db: Path) -> None:
    """`window=2` must be the integer 2: `2` and `"2"` are different artifacts."""
    entry = run_json(
        "show", "--db", str(db), "--json", "sma", "symbol=AAPL", "interval=1d", "window=2"
    )
    assert entry["key"]["window"] == 2

    assert run("show", "--db", str(db), "sma", "symbol=AAPL", "interval=1d", 'window="2"')[0] == 1


def test_show_needs_something_to_address(db: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("show", "--db", str(db))[0] == 1
    assert "--id" in capsys.readouterr().err


def test_a_malformed_key_pair_is_rejected(db: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("show", "--db", str(db), "sma", "symbol")[0] == 1
    assert "field=value" in capsys.readouterr().err


# --- stats -----------------------------------------------------------------


def test_stats_summarizes_without_materializing_every_row(db: Path) -> None:
    summary = run_json("stats", "--db", str(db), "--json")
    assert summary["artifacts"] == 4
    assert summary["invalid"] == 1
    assert summary["recipes"] == 2
    assert summary["dep_edges"] == 2
    assert summary["pending_tasks"] == 0
    assert {row["name"] for row in summary["by_recipe"]} == {"ohlcv", "sma"}
    assert summary["n_bytes"] > 0


def test_stats_renders_a_table(db: Path) -> None:
    code, text = run("stats", "--db", str(db))
    assert code == 0
    assert "4 artifacts (1 stale)" in text
    assert "ohlcv" in text and "json" in text


# --- verify ----------------------------------------------------------------


def test_verify_passes_on_an_intact_database(db: Path) -> None:
    code, text = run("verify", "--db", str(db))
    assert code == 0
    assert "checked 4" in text
    assert "all digests match" in text


def test_verify_catches_a_payload_that_no_longer_matches_its_digest(db: Path) -> None:
    """The one corruption the library cannot notice: nothing errors, the cache goes cold."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE artifact SET data = X'6465616462656566' WHERE id = 1")
    conn.commit()
    conn.close()

    code, text = run("verify", "--db", str(db))
    assert code == 1, "a mismatch is a failure, so a script can act on it"
    assert "1 MISMATCH" in text

    report = run_json("verify", "--db", str(db), "--json")
    assert report["checked"] == 4
    (bad,) = report["mismatches"]
    assert bad["id"] == 1
    assert bad["recorded_digest"] != bad["actual_digest"]


def test_verify_can_be_narrowed(db: Path) -> None:
    assert run_json("verify", "--db", str(db), "--json", "--recipe", "sma")["checked"] == 2
    assert run_json("verify", "--db", str(db), "--json", "--limit", "1")["checked"] == 1


# --- cat -------------------------------------------------------------------


def test_cat_decodes_without_recipes_because_the_codec_is_recorded(db: Path) -> None:
    """The codec is a property of the recipe *row*, so decoding needs no imports."""
    (row, *_) = run_json("ls", "--db", str(db), "--json", "--recipe", "sma")
    code, text = run("cat", "--db", str(db), "--id", str(row["id"]))
    assert code == 0
    assert "sma" in text
    assert len(REGISTRY) == 0


def test_cat_json_and_raw(db: Path) -> None:
    (row, *_) = run_json("ls", "--db", str(db), "--json", "--recipe", "ohlcv")
    value = run_json("cat", "--db", str(db), "--json", "--id", str(row["id"]))
    assert value["symbol"] == "AAPL"

    code, text = run("cat", "--db", str(db), "--id", str(row["id"]), "--raw")
    assert code == 0
    assert text == '{"close":[1,2,3],"symbol":"AAPL"}'


def test_cat_reports_a_codec_it_cannot_resolve(db: Path, capsys: pytest.CaptureFixture) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE recipe SET codec = 'pharos-custom' WHERE name = 'sma'")
    conn.commit()
    conn.close()

    (row, *_) = run_json("ls", "--db", str(db), "--json", "--recipe", "sma")
    assert run("cat", "--db", str(db), "--id", str(row["id"]))[0] == 1
    message = capsys.readouterr().err
    assert "pharos-custom" in message
    assert "--recipes MODULE" in message
    assert "--raw" in message


# --- plan ------------------------------------------------------------------


def test_plan_requires_recipes(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("plan", "--db", str(db), "sma", "symbol=AAPL")
    assert excinfo.value.code == 2


def test_plan_reports_a_current_graph_and_a_cold_one(db: Path, recipes_file: Path) -> None:
    report = run_json(
        "plan", "--db", str(db), "--json", "--recipes", str(recipes_file), "sma", "symbol=AAPL"
    )
    assert report["current"] is True
    assert report["absent"] == []
    assert {ref["recipe"] for ref in report["valid"]} == {"sma", "ohlcv"}

    cold = run_json(
        "plan", "--db", str(db), "--json", "--recipes", str(recipes_file), "sma", "symbol=NVDA"
    )
    assert cold["current"] is False
    assert [node["recipe"] for node in cold["absent"]] == ["sma"]
    assert cold["absent"][0]["path"], "the requesting path is reported"


def test_plan_exit_code_says_whether_work_is_needed(db: Path, recipes_file: Path) -> None:
    assert (
        run("plan", "--db", str(db), "--recipes", str(recipes_file), "sma", "symbol=AAPL")[0] == 0
    )
    assert (
        run("plan", "--db", str(db), "--recipes", str(recipes_file), "sma", "symbol=NVDA")[0] == 1
    )


# --- wiring ----------------------------------------------------------------


def test_the_database_can_come_from_the_environment(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATVIEW_DB", str(db))
    # The default is read at parser construction, so build it after setting the variable.
    assert run("stats")[0] == 0


def test_without_a_database_it_says_how_to_give_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATVIEW_DB", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        run("stats")
    assert excinfo.value.code == 2


def test_a_recipes_module_can_be_a_dotted_path_or_a_file(
    db: Path, recipes_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(recipes_file.parent))
    assert run("plan", "--db", str(db), "--recipes", "demo_recipes", "sma", "symbol=AAPL")[0] == 0
    REGISTRY.clear()
    assert (
        run("plan", "--db", str(db), "--recipes", str(recipes_file), "sma", "symbol=AAPL")[0] == 0
    )


def test_an_unimportable_recipes_module_is_reported(
    db: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run("plan", "--db", str(db), "--recipes", "not_a_real_module", "sma")[0] == 1
    assert "not_a_real_module" in capsys.readouterr().err

    assert run("plan", "--db", str(db), "--recipes", "/tmp/nowhere.py", "sma")[0] == 1
    assert "no such file" in capsys.readouterr().err


# --- lineage ---------------------------------------------------------------


def _sma_id(db: Path, symbol: str = "AAPL") -> int:
    store = SqliteStore(db, read_only=True)
    try:
        (ref,) = [r for r in store.list_refs("sma") if symbol in r.key_json]
        return ref.id
    finally:
        store.close()


def test_lineage_shows_what_an_artifact_was_built_from(db: Path) -> None:
    code, text = run("lineage", "--db", str(db), "--id", str(_sma_id(db)))
    assert code == 0
    assert "sma v3" in text
    assert "ohlcv v1" in text
    assert "└─" in text


def test_lineage_needs_no_recipes_module(db: Path) -> None:
    """The metadata division from D37: the DAG is in the file, so nothing is imported."""
    assert REGISTRY.names() == []
    assert run("lineage", "--db", str(db), "--id", str(_sma_id(db)))[0] == 0


def test_lineage_addresses_an_artifact_the_way_show_does(db: Path, recipes_file: Path) -> None:
    by_id, _ = run("lineage", "--db", str(db), "--id", str(_sma_id(db)), "--format", "json")
    by_key, _ = run(
        "lineage",
        "--db",
        str(db),
        "--recipes",
        str(recipes_file),
        "sma",
        "symbol=AAPL",
        "--format",
        "json",
    )
    assert by_id == by_key == 0


@pytest.mark.parametrize("fmt", ["tree", "json", "dot", "mermaid"])
def test_lineage_renders_every_format(db: Path, fmt: str) -> None:
    code, text = run("lineage", "--db", str(db), "--id", str(_sma_id(db)), "--format", fmt)
    assert code == 0
    assert text.strip()


def test_lineage_json_is_the_same_whichever_flag_asks_for_it(db: Path) -> None:
    artifact_id = str(_sma_id(db))
    assert (
        run("lineage", "--db", str(db), "--id", artifact_id, "--format", "json")[1]
        == run("lineage", "--db", str(db), "--id", artifact_id, "--json")[1]
    )


def test_lineage_depth_bounds_the_walk(db: Path) -> None:
    payload = run_json(
        "lineage", "--db", str(db), "--id", str(_sma_id(db)), "--depth", "0", "--json"
    )
    assert len(payload["nodes"]) == 1


def test_lineage_of_an_unknown_id_says_so(db: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("lineage", "--db", str(db), "--id", "999")[0] == 1
    assert "no artifact with id 999" in capsys.readouterr().err


# --- impact ----------------------------------------------------------------


def _ohlcv_id(db: Path, symbol: str = "AAPL") -> int:
    store = SqliteStore(db, read_only=True)
    try:
        (ref,) = [r for r in store.list_refs("ohlcv") if symbol in r.key_json]
        return ref.id
    finally:
        store.close()


def test_impact_of_a_change_counts_by_recipe(db: Path) -> None:
    code, text = run("impact", "--db", str(db), "--changed", str(_ohlcv_id(db)))
    assert code == 0
    assert "would be marked stale: 1" in text
    assert "sma v3" in text


def test_impact_summarizes_unless_asked_to_enumerate(db: Path) -> None:
    summary = run_json("impact", "--db", str(db), "--changed", str(_ohlcv_id(db)), "--json")
    listed = run_json("impact", "--db", str(db), "--changed", str(_ohlcv_id(db)), "--all", "--json")
    assert summary["groups"][0]["artifacts"] == []
    assert len(listed["groups"][0]["artifacts"]) == 1


def test_impact_of_a_bump_states_that_nothing_goes_stale(db: Path) -> None:
    """The wording is the feature: rule 8 is what a new reader most needs explained."""
    code, text = run("impact", "--db", str(db), "--bump", "ohlcv")
    assert code == 0
    assert "marks NOTHING stale" in text
    assert "rule 8" in text
    assert "become unreachable" in text
    assert "re-resolve on their next read" in text


def test_impact_of_a_drop_shows_deletions_and_invalidations(db: Path) -> None:
    code, text = run("impact", "--db", str(db), "--drop", "ohlcv")
    assert code == 0
    assert "deleted: 2" in text
    assert "marked stale: 2" in text


def test_impact_drop_does_not_drop_anything(db: Path) -> None:
    """`drop` is not a command here; seeing what it would do is the useful half."""
    before = run_json("stats", "--db", str(db), "--json")
    run("impact", "--db", str(db), "--drop", "ohlcv", "--all")
    assert run_json("stats", "--db", str(db), "--json") == before


def test_impact_takes_one_question_at_a_time(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("impact", "--db", str(db), "--changed", "1", "--bump", "ohlcv")
    assert excinfo.value.code == 2


def test_impact_needs_a_question(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("impact", "--db", str(db))
    assert excinfo.value.code == 2


def test_impact_version_only_goes_with_drop(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("impact", "--db", str(db), "--bump", "ohlcv", "--version", "1")
    assert excinfo.value.code == 2


def test_drawing_an_impact_set_requires_asking_for_it(
    db: Path, capsys: pytest.CaptureFixture
) -> None:
    """A default that renders 255,000 nodes to DOT would help nobody."""
    assert (
        run("impact", "--db", str(db), "--changed", str(_ohlcv_id(db)), "--format", "dot")[0] == 1
    )
    assert "needs --all" in capsys.readouterr().err

    code, text = run(
        "impact", "--db", str(db), "--changed", str(_ohlcv_id(db)), "--all", "--format", "dot"
    )
    assert code == 0
    assert "digraph" in text


def test_drawing_an_impact_set_emits_one_document(db: Path) -> None:
    """Two `graph LR` blocks are not a Mermaid document, and a bump has three groups."""
    _code, dot = run("impact", "--db", str(db), "--bump", "ohlcv", "--all", "--format", "dot")
    assert dot.count("digraph") == 1
    _code, mermaid = run(
        "impact", "--db", str(db), "--bump", "ohlcv", "--all", "--format", "mermaid"
    )
    assert mermaid.count("graph LR") == 1
    assert mermaid.startswith("%%"), "the group labels are comments in the emitted language"


# --- cost ------------------------------------------------------------------


def test_cost_reports_the_closure_and_makes_the_deduplication_visible(db: Path) -> None:
    code, text = run("cost", "--db", str(db), "--id", str(_sma_id(db)))
    assert code == 0
    assert "embodied compute" in text
    assert "2 value(s) in 2 artifact(s)" in text
    assert "per value, not per artifact" in text


def test_cost_can_report_the_critical_path(db: Path) -> None:
    code, text = run("cost", "--db", str(db), "--id", str(_sma_id(db)), "--critical-path")
    assert code == 0
    assert "critical path" in text


def test_cost_by_recipe_rolls_up_without_walking_the_graph(db: Path) -> None:
    code, text = run("cost", "--db", str(db), "--by-recipe")
    assert code == 0
    assert "ohlcv" in text and "sma" in text
    assert "values" in text


def test_cost_by_recipe_json_counts_values_and_artifacts_separately(db: Path) -> None:
    rows = {
        row["recipe"]: row for row in run_json("cost", "--db", str(db), "--by-recipe", "--json")
    }
    assert rows["sma"]["artifacts"] == 2
    assert rows["sma"]["values"] == 2


def test_cost_needs_an_artifact_or_by_recipe(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("cost", "--db", str(db))
    assert excinfo.value.code == 2


def test_cost_by_recipe_takes_no_artifact(db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run("cost", "--db", str(db), "--by-recipe", "--id", "1")
    assert excinfo.value.code == 2


def test_the_graph_commands_never_write(db: Path) -> None:
    """Every command here is a question. None of them may touch the file."""
    before = db.stat().st_mtime_ns, db.read_bytes()
    run("lineage", "--db", str(db), "--id", str(_sma_id(db)))
    run("impact", "--db", str(db), "--changed", str(_ohlcv_id(db)), "--all")
    run("impact", "--db", str(db), "--bump", "ohlcv")
    run("cost", "--db", str(db), "--by-recipe")
    run("cost", "--db", str(db), "--id", str(_sma_id(db)), "--critical-path")
    assert (db.stat().st_mtime_ns, db.read_bytes()) == before
