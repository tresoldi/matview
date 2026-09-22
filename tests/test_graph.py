"""Graph queries: the store's two walks, and `matview.graph` on top of them.

The correctness trap these tests exist to pin down is the cost one. `compute_ms` is
recorded once per *artifact*, and a partitioned value is several artifacts that each
carry the cost of the single compute that produced them all. Anything that sums the
column over artifact rows multiplies such a value by its part count — silently, and with
no symptom but a wrong number. `test_cost_*` below assert the deduplication end to end.
"""

from __future__ import annotations

from typing import Any

import pytest

from matview import SqliteStore
from matview.errors import MatviewError
from matview.graph import (
    ArtifactGraph,
    GraphStore,
    critical_path,
    embodied_cost,
    impact_of_bump,
    impact_of_change,
    impact_of_drop,
    lineage,
    recipe_costs,
)
from matview.types import DepEdge

from .conftest import make_artifact


def _edge(artifact: Any, artifact_id: int) -> DepEdge:
    return DepEdge(
        recipe=artifact.recipe,
        version=artifact.version,
        key_hash=artifact.key_hash,
        part=artifact.part,
        digest=artifact.digest,
        artifact_id=artifact_id,
        key_json=artifact.key_json,
    )


def _write(
    store: SqliteStore,
    recipe: str,
    *,
    version: int = 1,
    data: bytes = b"{}",
    key: dict[str, object] | None = None,
    part: str | None = None,
    compute_ms: int | None = 1,
    on: tuple[tuple[Any, int], ...] = (),
) -> tuple[Any, int]:
    """Write one artifact over the given `(artifact, id)` dependencies, as the scheduler would."""
    from dataclasses import replace

    deps = tuple(_edge(a, i) for a, i in on)
    artifact = replace(
        make_artifact(recipe=recipe, version=version, data=data, key=key, part=part, deps=deps),
        compute_ms=compute_ms,
    )
    return artifact, store.store(artifact, deps).artifact_id


@pytest.fixture
def chain(store: SqliteStore) -> tuple[int, int, int]:
    """raw <- mid <- top, one artifact each."""
    raw, raw_id = _write(store, "raw", data=b'{"v":1}', compute_ms=100)
    mid, mid_id = _write(store, "mid", data=b'{"v":2}', compute_ms=10, on=((raw, raw_id),))
    _top, top_id = _write(store, "top", data=b'{"v":3}', compute_ms=1, on=((mid, mid_id),))
    return raw_id, mid_id, top_id


@pytest.fixture
def diamond(store: SqliteStore) -> dict[str, int]:
    """One shared root read by two middles, both read by one top."""
    raw, raw_id = _write(store, "raw", data=b'{"v":1}', compute_ms=5)
    left, left_id = _write(store, "left", data=b'{"l":1}', compute_ms=50, on=((raw, raw_id),))
    right, right_id = _write(store, "right", data=b'{"r":1}', compute_ms=7, on=((raw, raw_id),))
    _top, top_id = _write(
        store, "top", data=b'{"t":1}', compute_ms=1, on=((left, left_id), (right, right_id))
    )
    return {"raw": raw_id, "left": left_id, "right": right_id, "top": top_id}


# --- the store's walks ------------------------------------------------------


def test_inputs_of_returns_the_closure_and_excludes_the_roots(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    raw_id, mid_id, top_id = chain
    assert store.inputs_of([top_id]) == [raw_id, mid_id]
    assert top_id not in store.inputs_of([top_id])


def test_inputs_of_a_root_is_empty(store: SqliteStore, chain: tuple[int, int, int]) -> None:
    raw_id, _mid_id, _top_id = chain
    assert store.inputs_of([raw_id]) == []


def test_inputs_of_depth_stops_after_one_level(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    raw_id, mid_id, top_id = chain
    assert store.inputs_of([top_id], depth=1) == [mid_id]
    assert store.inputs_of([top_id], depth=2) == [raw_id, mid_id]
    assert store.inputs_of([top_id], depth=99) == [raw_id, mid_id]


def test_inputs_of_a_diamond_reports_the_shared_root_once(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    assert store.inputs_of([diamond["top"]]) == sorted(
        [diamond["raw"], diamond["left"], diamond["right"]]
    )


def test_dependents_of_takes_a_depth_too(store: SqliteStore, diamond: dict[str, int]) -> None:
    assert store.dependents_of([diamond["raw"]], depth=1) == sorted(
        [diamond["left"], diamond["right"]]
    )
    assert len(store.dependents_of([diamond["raw"]])) == 3


def test_the_walks_are_mirror_images(store: SqliteStore, chain: tuple[int, int, int]) -> None:
    raw_id, mid_id, top_id = chain
    assert store.inputs_of([top_id]) == [raw_id, mid_id]
    assert store.dependents_of([raw_id]) == [mid_id, top_id]


def test_neither_walk_hangs_on_a_cycle(store: SqliteStore, chain: tuple[int, int, int]) -> None:
    """The scheduler forbids cycles; a corrupted file can still contain one.

    A walk that loops forever on a malformed database is worse than one that returns a
    slightly odd answer, because the process has to be killed to find out anything at all.
    """
    raw_id, mid_id, top_id = chain
    store._connect().execute(
        "INSERT INTO dep (artifact_id, dep_artifact_id, dep_digest) VALUES (?, ?, ?)",
        (raw_id, top_id, "deadbeef"),
    )
    assert store.inputs_of([top_id]) == sorted([raw_id, mid_id])
    assert store.inputs_of([top_id], depth=10) == sorted([raw_id, mid_id])
    assert store.dependents_of([raw_id]) == sorted([mid_id, top_id])
    assert store.dependents_of([raw_id], depth=10) == sorted([mid_id, top_id])


def test_deps_of_many_agrees_with_repeated_deps_of(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    ids = sorted(diamond.values())
    assert store.deps_of_many(ids) == {i: store.deps_of(i) for i in ids}


def test_deps_of_many_keys_every_id_it_was_given(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """A root maps to an empty list, not to a missing key: the caller indexes the result."""
    edges = store.deps_of_many([diamond["raw"], diamond["top"]])
    assert set(edges) == {diamond["raw"], diamond["top"]}
    assert edges[diamond["raw"]] == []
    assert len(edges[diamond["top"]]) == 2


def test_deps_of_many_of_nothing_is_empty(store: SqliteStore) -> None:
    assert store.deps_of_many([]) == {}


def test_a_sqlite_store_satisfies_the_graph_store_protocol(store: SqliteStore) -> None:
    """`GraphStore` is deliberately not `Store`; `SqliteStore` satisfies both."""
    assert isinstance(store, GraphStore)


# --- lineage ----------------------------------------------------------------


def test_lineage_has_the_closure_and_the_edges_inside_it(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    graph = lineage(store, diamond["top"])
    assert set(graph.nodes) == set(diamond.values())
    assert graph.roots == (diamond["top"],)
    assert (diamond["top"], diamond["left"]) in graph.edges
    assert (diamond["left"], diamond["raw"]) in graph.edges


def test_lineage_depth_bounds_both_nodes_and_edges(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """A bounded walk must be a closed picture, not one with arrows to nothing."""
    graph = lineage(store, diamond["top"], depth=1)
    assert set(graph.nodes) == {diamond["top"], diamond["left"], diamond["right"]}
    assert all(a in graph.nodes and b in graph.nodes for a, b in graph.edges)
    assert diamond["raw"] not in {b for _a, b in graph.edges}


def test_lineage_of_an_unknown_id_says_so(store: SqliteStore) -> None:
    with pytest.raises(MatviewError, match="no artifact with id 999"):
        lineage(store, 999)


def test_lineage_needs_no_registry(store: SqliteStore, diamond: dict[str, int]) -> None:
    """D37's division: this is a metadata query, so nothing has to be imported."""
    from matview.registry import REGISTRY

    REGISTRY.clear()
    assert len(lineage(store, diamond["top"])) == 4


def test_the_tree_shows_a_shared_node_once_and_marks_the_repeat(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    tree = lineage(store, diamond["top"]).to_tree()
    assert tree.count("(shown above)") == 1
    assert tree.count(f"[{diamond['raw']}] raw") == 2


def test_the_tree_shows_staleness(store: SqliteStore, diamond: dict[str, int]) -> None:
    """A stale node in someone's lineage is very often what they opened this to find."""
    store.invalidate([diamond["left"]])
    tree = lineage(store, diamond["top"]).to_tree()
    assert "STALE" in tree


@pytest.mark.parametrize("fmt", ["tree", "json", "dot", "mermaid"])
def test_every_format_renders_every_node(
    store: SqliteStore, diamond: dict[str, int], fmt: str
) -> None:
    text = lineage(store, diamond["top"]).render(fmt)
    assert all(str(node_id) in text for node_id in diamond.values())


def test_an_unknown_format_is_an_error(store: SqliteStore, diamond: dict[str, int]) -> None:
    with pytest.raises(MatviewError, match="unknown format"):
        lineage(store, diamond["top"]).render("svg")


def test_dot_and_mermaid_point_the_arrows_the_way_data_flows(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    raw_id, mid_id, top_id = chain
    graph = lineage(store, top_id)
    assert f"n{raw_id} -> n{mid_id};" in graph.to_dot()
    assert f"n{raw_id} --> n{mid_id}" in graph.to_mermaid()


def test_dot_escapes_the_quotes_in_a_key(store: SqliteStore) -> None:
    _artifact, artifact_id = _write(store, "quoted", key={"symbol": "AAPL"})
    assert '\\"symbol\\"' in lineage(store, artifact_id).to_dot()


def test_the_graph_json_round_trips_through_json(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    import json

    payload = json.loads(lineage(store, diamond["top"]).render("json"))
    assert payload["direction"] == "inputs"
    assert len(payload["nodes"]) == 4
    assert {"dependent": diamond["top"], "dependency": diamond["left"]} in payload["edges"]


def test_an_empty_graph_renders_without_erroring() -> None:
    empty = ArtifactGraph(nodes={}, edges=(), roots=())
    assert empty.to_tree() == ""
    assert "digraph" in empty.to_dot()
    assert empty.to_mermaid() == "graph LR"


# --- impact -----------------------------------------------------------------


def test_impact_of_change_counts_the_cascade_by_recipe(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    report = impact_of_change(store, diamond["raw"])
    assert report.question == "changed"
    group = report.groups[0]
    assert group.total == 3
    assert {(c.recipe, c.artifacts) for c in group.by_recipe} == {
        ("left", 1),
        ("right", 1),
        ("top", 1),
    }


def test_impact_of_change_summarizes_rather_than_enumerating_by_default(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """The default must be a summary: one shared root's impact can be 255,000 artifacts."""
    assert impact_of_change(store, diamond["raw"]).groups[0].refs == ()
    assert len(impact_of_change(store, diamond["raw"], enumerate_all=True).groups[0].refs) == 3


def test_impact_of_change_limit_bounds_the_listing_but_not_the_count(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    group = impact_of_change(store, diamond["raw"], enumerate_all=True, limit=2).groups[0]
    assert group.total == 3
    assert len(group.refs) == 2
    assert group.truncated


def test_impact_of_change_separates_what_is_already_stale(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """An already-stale dependent is not additional damage, and the cascade skips it."""
    store.invalidate([diamond["left"]])
    report = impact_of_change(store, diamond["raw"])
    assert sum(c.stale for c in report.groups[0].by_recipe) == 2  # left, and top below it
    assert "1 of them are valid today" in "\n".join(report.notes)


def test_impact_of_change_respects_depth(store: SqliteStore, diamond: dict[str, int]) -> None:
    assert impact_of_change(store, diamond["raw"], depth=1).groups[0].total == 2


def test_impact_of_a_bump_says_plainly_that_nothing_goes_stale(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """Rule 8 is the part of the spec that confuses every new reader, so it is stated."""
    report = impact_of_bump(store, "raw")
    notes = "\n".join(report.notes)
    assert "marks NOTHING stale" in notes
    assert "rule 8" in notes
    assert "early cutoff" in notes


def test_a_bump_reports_what_becomes_unreachable_and_what_re_resolves(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    unreachable, re_resolve, superseded = impact_of_bump(store, "raw").groups
    assert unreachable.ids == (diamond["raw"],)
    assert set(re_resolve.ids) == {diamond["left"], diamond["right"]}
    assert superseded.total == 0


def test_a_bump_reports_only_direct_dependents_as_re_resolving(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """The transitive cascade is conditional on the bytes differing, so it is not counted."""
    _unreachable, re_resolve, _superseded = impact_of_bump(store, "raw").groups
    assert diamond["top"] not in re_resolve.ids


def test_a_bump_counts_artifacts_earlier_bumps_already_left_behind(store: SqliteStore) -> None:
    _old, _old_id = _write(store, "raw", version=1, data=b'{"v":1}')
    _new, _new_id = _write(store, "raw", version=2, data=b'{"v":2}')
    unreachable, _re_resolve, superseded = impact_of_bump(store, "raw").groups
    assert unreachable.total == 1
    assert superseded.total == 1
    assert "cache.gc()" in "\n".join(impact_of_bump(store, "raw").notes)


def test_a_bump_of_an_unknown_recipe_says_so(store: SqliteStore) -> None:
    with pytest.raises(MatviewError, match="no recipe named 'nope'"):
        impact_of_bump(store, "nope")


def test_impact_of_a_drop_reports_deletions_and_invalidations_separately(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    deleted, invalidated = impact_of_drop(store, "left").groups
    assert deleted.ids == (diamond["left"],)
    assert invalidated.ids == (diamond["top"],)
    assert "delete" in "\n".join(impact_of_drop(store, "left").notes)


def test_impact_of_a_drop_can_be_pinned_to_one_version(store: SqliteStore) -> None:
    _write(store, "raw", version=1, data=b'{"v":1}')
    _write(store, "raw", version=2, data=b'{"v":2}')
    assert impact_of_drop(store, "raw").groups[0].total == 2
    assert impact_of_drop(store, "raw", version=2).groups[0].total == 1


def test_impact_of_a_drop_that_would_delete_nothing_says_so(store: SqliteStore) -> None:
    with pytest.raises(MatviewError, match="no stored artifacts of nope"):
        impact_of_drop(store, "nope")


def test_the_impact_graph_is_the_union_of_every_group(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """A bump's unreachable set and its re-resolving dependents are joined by real edges."""
    from matview.graph import impact_graph

    report = impact_of_bump(store, "raw")
    graph = impact_graph(store, report, diamond["raw"])
    assert set(graph.nodes) == {diamond["raw"], diamond["left"], diamond["right"]}
    assert (diamond["left"], diamond["raw"]) in graph.edges
    assert graph.direction == "dependents"


def test_impact_changes_nothing(store: SqliteStore, diamond: dict[str, int]) -> None:
    """Every one of these is a question, not an instruction. None of them may write."""
    before = store.summary()
    impact_of_change(store, diamond["raw"], enumerate_all=True)
    impact_of_bump(store, "raw", enumerate_all=True)
    impact_of_drop(store, "left", enumerate_all=True)
    assert store.summary() == before
    assert all(store.ref_by_id(i) is not None for i in diamond.values())
    assert all(store.ref_by_id(i).valid for i in diamond.values())  # type: ignore[union-attr]


# --- cost -------------------------------------------------------------------


def test_cost_sums_the_input_closure(store: SqliteStore, chain: tuple[int, int, int]) -> None:
    _raw_id, _mid_id, top_id = chain
    report = embodied_cost(store, top_id)
    assert report.compute_ms == 111
    assert report.own_compute_ms == 1
    assert report.values == 3


def test_cost_counts_a_shared_dependency_once(store: SqliteStore, diamond: dict[str, int]) -> None:
    """The diamond's root is read twice and cost 5 ms once."""
    assert embodied_cost(store, diamond["top"]).compute_ms == 5 + 50 + 7 + 1


def test_cost_counts_a_partitioned_value_once(store: SqliteStore) -> None:
    """The trap this whole feature is built around.

    Three parts of one value each record the 39 ms that produced all three. Summing the
    artifact column gives 117; the value cost is 39. Nothing errors either way, which is
    exactly why this is asserted rather than assumed.
    """
    from dataclasses import replace

    parts = [
        replace(
            make_artifact(
                recipe="years", key={"symbol": "AAPL"}, part=year, data=f'{{"{year}":1}}'.encode()
            ),
            compute_ms=39,
        )
        for year in ("2023", "2024", "2025")
    ]
    ids = [r.artifact_id for r in store.store_parts(parts, ())]
    _consumer, consumer_id = _write(
        store,
        "consumer",
        data=b'{"c":1}',
        compute_ms=2,
        on=tuple((part, i) for part, i in zip(parts, ids, strict=True)),
    )

    report = embodied_cost(store, consumer_id)
    assert report.artifacts == 4
    assert report.values == 2
    assert report.compute_ms == 41  # not 2 + 39 * 3
    assert sum((store.ref_by_id(i).compute_ms or 0) for i in ids) == 117  # type: ignore[union-attr]


def test_the_per_recipe_rollup_counts_values_not_artifacts(store: SqliteStore) -> None:
    from dataclasses import replace

    parts = [
        replace(
            make_artifact(
                recipe="years", key={"symbol": "AAPL"}, part=year, data=f'{{"{year}":1}}'.encode()
            ),
            compute_ms=39,
        )
        for year in ("2023", "2024", "2025")
    ]
    store.store_parts(parts, ())
    (row,) = [c for c in recipe_costs(store) if c.recipe == "years"]
    assert row.artifacts == 3
    assert row.values == 1
    assert row.compute_ms == 39
    assert row.mean_compute_ms == 39.0


def test_the_global_summary_counts_values_not_artifacts(store: SqliteStore) -> None:
    """`summary()` overcounted partitioned values before this work; it must not again."""
    from dataclasses import replace

    parts = [
        replace(
            make_artifact(recipe="years", part=year, data=f'{{"{year}":1}}'.encode()), compute_ms=39
        )
        for year in ("2023", "2024", "2025")
    ]
    store.store_parts(parts, ())
    summary = store.summary()
    assert summary["artifacts"] == 3
    assert summary["values"] == 1
    assert summary["compute_ms"] == 39


def test_cost_reports_values_with_no_recorded_cost_as_a_floor(store: SqliteStore) -> None:
    """An unmeasured value contributes nothing, and the report says so rather than hiding it."""
    raw, raw_id = _write(store, "raw", data=b'{"v":1}', compute_ms=None)
    _top, top_id = _write(store, "top", data=b'{"t":1}', compute_ms=4, on=((raw, raw_id),))
    report = embodied_cost(store, top_id)
    assert report.compute_ms == 4
    assert report.unmeasured_values == 1
    assert "floor" in report.describe()


def test_cost_respects_depth(store: SqliteStore, chain: tuple[int, int, int]) -> None:
    _raw_id, _mid_id, top_id = chain
    assert embodied_cost(store, top_id, depth=1).compute_ms == 11
    assert embodied_cost(store, top_id, depth=1).depth == 1


def test_cost_of_an_unknown_id_says_so(store: SqliteStore) -> None:
    with pytest.raises(MatviewError, match="no artifact with id 999"):
        embodied_cost(store, 999)


def test_the_critical_path_is_the_expensive_chain_not_the_longest(
    store: SqliteStore, diamond: dict[str, int]
) -> None:
    """Both branches are two steps. The point of the question is which one dominates."""
    path = critical_path(store, diamond["top"])
    assert [step.ref.id for step in path] == [diamond["top"], diamond["left"], diamond["raw"]]
    assert [step.compute_ms for step in path] == [1, 50, 5]
    assert [step.cumulative_ms for step in path] == [1, 51, 56]


def test_the_critical_path_of_a_root_is_just_that_root(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    raw_id, _mid_id, _top_id = chain
    path = critical_path(store, raw_id)
    assert [step.ref.id for step in path] == [raw_id]


def test_the_critical_path_terminates_on_a_cycle(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    raw_id, _mid_id, top_id = chain
    store._connect().execute(
        "INSERT INTO dep (artifact_id, dep_artifact_id, dep_digest) VALUES (?, ?, ?)",
        (raw_id, top_id, "deadbeef"),
    )
    assert len(critical_path(store, top_id)) <= 3


def test_the_report_makes_the_deduplication_visible(
    store: SqliteStore, chain: tuple[int, int, int]
) -> None:
    """Reporting the value count alongside the total is what keeps the number checkable."""
    text = embodied_cost(store, chain[2], with_critical_path=True).describe()
    assert "3 value(s) in 3 artifact(s)" in text
    assert "per value, not per artifact" in text
    assert "critical path" in text
