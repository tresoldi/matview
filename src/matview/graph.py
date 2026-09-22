"""Questions about the recorded DAG: lineage, impact and cost.

matview records a complete observed dependency graph as it computes, and until now
offered almost no way to interrogate it. These are the three questions worth asking of
it:

- **Lineage** — which inputs produced this number? The audit question, and the
  reproducibility question.
- **Impact** — what breaks if this changes? Asked of a changed artifact, a recipe
  version bump, and a drop.
- **Cost** — what did this number cost to produce, and which step dominates?

Everything here is a metadata query against the store. Nothing resolves a recipe, so
all of it works against a database whose recipes this process has never imported — the
same division `matview.cli` draws. Nothing here writes.

The renderers live here rather than on the store on purpose. A store persists; it should
not know about Graphviz, and the `Store` protocol should not grow methods a remote
backend has no business implementing. `GraphStore` below is the read surface these
queries actually need, satisfied structurally by `SqliteStore`. See DECISIONS.md D49.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from matview.errors import MatviewError
from matview.types import ArtifactRef, DepEdge

__all__ = [
    "ArtifactGraph",
    "CostReport",
    "Direction",
    "GraphStore",
    "ImpactGroup",
    "ImpactReport",
    "PathStep",
    "RecipeCost",
    "RecipeCount",
    "critical_path",
    "embodied_cost",
    "impact_graph",
    "impact_of_bump",
    "impact_of_change",
    "impact_of_drop",
    "lineage",
    "recipe_costs",
    "ref_to_dict",
]

#: Which way along `dep` a graph was walked. `"inputs"` is the lineage direction, up
#: towards raw inputs; `"dependents"` is the invalidation direction, down towards
#: consumers. An `ArtifactGraph` carries it so a renderer knows which end of an edge to
#: descend from, and so the rendering can say which question it is answering.
Direction = Literal["inputs", "dependents"]


@runtime_checkable
class GraphStore(Protocol):
    """The store reads these queries need.

    Declared here rather than added to `Store` so that the persistence seam stays the
    set of operations a backend must implement to hold artifacts. A backend that also
    wants to answer graph questions satisfies this structurally, as `SqliteStore` does.
    """

    def ref_by_id(self, artifact_id: int) -> ArtifactRef | None: ...

    def refs_by_ids(self, artifact_ids: Sequence[int]) -> list[ArtifactRef]: ...

    def refs_by_key_hash(self, key_hash: str, recipe: str | None = None) -> list[ArtifactRef]: ...

    def deps_of_many(self, artifact_ids: Sequence[int]) -> dict[int, list[DepEdge]]: ...

    def inputs_of(self, artifact_ids: Sequence[int], *, depth: int | None = ...) -> list[int]: ...

    def dependents_of(
        self, artifact_ids: Sequence[int], *, depth: int | None = ...
    ) -> list[int]: ...

    def ids_of_recipe(self, recipe: str, version: int | None = ...) -> list[int]: ...

    def counts_by_recipe(self, artifact_ids: Sequence[int]) -> list[dict[str, Any]]: ...

    def recipe_rows(self) -> list[dict[str, Any]]: ...

    def recipe_cost_rows(self) -> list[dict[str, Any]]: ...


def ref_to_dict(ref: ArtifactRef) -> dict[str, Any]:
    """One artifact's metadata as JSON-ready data, with its key parsed back to an object.

    One shape for every machine-readable rendering in the library, so that a node in a
    lineage graph and a row from `matview ls` describe an artifact the same way.
    """
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


def _value_of(ref: ArtifactRef) -> tuple[str, int, str]:
    """The identity a *value* has, as distinct from an artifact row.

    A partitioned value is several artifacts sharing one `(recipe, version, key_hash)`.
    Any cost aggregate must group on this and never on the artifact id; see the note on
    `embodied_cost`.
    """
    return (ref.recipe, ref.version, ref.key_hash)


# --- the graph value type ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactGraph:
    """A walked piece of the recorded DAG, with no reference to the store it came from.

    A plain value object on purpose: build it once, render it four ways. `edges` is
    always `(dependent, dependency)` whichever direction the walk ran in, because that
    is the direction the `dep` table records and reversing it in storage would make the
    two walks disagree about what an edge means. `direction` says which end of an edge a
    renderer should descend from.
    """

    #: Every artifact in the walk, including the roots, by row id.
    nodes: Mapping[int, ArtifactRef]
    #: `(dependent, dependency)` pairs, both ends present in `nodes`.
    edges: tuple[tuple[int, int], ...]
    #: The artifacts the walk started from.
    roots: tuple[int, ...]
    direction: Direction = "inputs"
    #: True when a `limit` cut the walk short, so a reader knows the picture is partial.
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.nodes)

    def children_of(self, artifact_id: int) -> tuple[int, ...]:
        """The nodes one step further from the roots, in the walk's own direction."""
        if self.direction == "inputs":
            found = [b for a, b in self.edges if a == artifact_id]
        else:
            found = [a for a, b in self.edges if b == artifact_id]
        return tuple(sorted(set(found)))

    # -- renderings ---------------------------------------------------------

    def to_tree(self) -> str:
        """An indented tree, roots first.

        The DAG is not a tree, so a node reachable by more than one path is printed
        again and marked rather than expanded twice — which both keeps the output
        bounded on a diamond and makes the sharing visible.
        """
        lines: list[str] = []
        expanded: set[int] = set()

        def walk(node_id: int, prefix: str, connector: str) -> None:
            ref = self.nodes.get(node_id)
            label = f"[{node_id}] (outside this walk)" if ref is None else _label(ref)
            seen = node_id in expanded
            lines.append(prefix + connector + label + ("  (shown above)" if seen else ""))
            if seen or ref is None:
                return
            expanded.add(node_id)
            children = self.children_of(node_id)
            below = prefix if not connector else prefix + (_SPACE if connector == _LAST else _BAR)
            for i, child in enumerate(children):
                walk(child, below, _LAST if i == len(children) - 1 else _TEE)

        for root in self.roots:
            walk(root, "", "")
        if self.truncated:
            lines.append("... (truncated)")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        """The graph as data: nodes, edges and roots, with the walk's direction."""
        return {
            "direction": self.direction,
            "roots": list(self.roots),
            "truncated": self.truncated,
            "nodes": [ref_to_dict(self.nodes[i]) for i in sorted(self.nodes)],
            "edges": [{"dependent": a, "dependency": b} for a, b in self.edges],
        }

    def to_dot(self) -> str:
        """Graphviz DOT. Arrows run the way data does: dependency to dependent."""
        out = [
            f"digraph {self.direction} {{",
            "  rankdir=LR;",
            '  node [shape=box, fontname="Helvetica", fontsize=10];',
        ]
        roots = set(self.roots)
        for node_id in sorted(self.nodes):
            ref = self.nodes[node_id]
            attrs = [f'label="{_dot_escape(_multiline_label(ref))}"']
            if not ref.valid:
                attrs.append("style=dashed")
            if node_id in roots:
                attrs.append("penwidth=2")
            out.append(f"  n{node_id} [{', '.join(attrs)}];")
        for dependent, dependency in self.edges:
            out.append(f"  n{dependency} -> n{dependent};")
        out.append("}")
        return "\n".join(out)

    def to_mermaid(self) -> str:
        """Mermaid `graph LR`, with the same arrow direction as `to_dot`."""
        out = ["graph LR"]
        for node_id in sorted(self.nodes):
            ref = self.nodes[node_id]
            label = _mermaid_escape(_multiline_label(ref)).replace("\n", "<br/>")
            out.append(f'  n{node_id}["{label}"]')
        for dependent, dependency in self.edges:
            out.append(f"  n{dependency} --> n{dependent}")
        stale = [i for i in sorted(self.nodes) if not self.nodes[i].valid]
        if stale:
            out.append("  classDef stale stroke-dasharray: 4 3;")
            out.append(f"  class {','.join(f'n{i}' for i in stale)} stale;")
        return "\n".join(out)

    def render(self, fmt: str) -> str:
        """Render by format name: `tree`, `json`, `dot` or `mermaid`."""
        if fmt == "tree":
            return self.to_tree()
        if fmt == "json":
            return json.dumps(self.to_json(), indent=2, sort_keys=True, default=str)
        if fmt == "dot":
            return self.to_dot()
        if fmt == "mermaid":
            return self.to_mermaid()
        raise MatviewError(f"unknown format {fmt!r}; choose tree, json, dot or mermaid")


#: Box-drawing pieces for `ArtifactGraph.to_tree`.
_TEE = "\u251c\u2500 "
_LAST = "\u2514\u2500 "
_BAR = "\u2502  "
_SPACE = "   "


def _label(ref: ArtifactRef) -> str:
    """One line describing a node: what it is, whether to trust it, and which bytes.

    Validity is on it because a stale node in someone's lineage is very often exactly
    what they opened the command to find.
    """
    part = "" if ref.part is None else f" part={ref.part!r}"
    state = "valid" if ref.valid else "STALE"
    return f"[{ref.id}] {ref.recipe} v{ref.version}{part} {ref.key_json} {state} {ref.digest[:12]}"


def _multiline_label(ref: ArtifactRef) -> str:
    part = "" if ref.part is None else f" part={ref.part}"
    state = "" if ref.valid else "\nSTALE"
    return f"{ref.recipe} v{ref.version}{part}\n{ref.key_json}\n{ref.digest[:12]}{state}"


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _mermaid_escape(text: str) -> str:
    return text.replace('"', "#quot;").replace("[", "#91;").replace("]", "#93;")


# --- lineage ----------------------------------------------------------------


def lineage(store: GraphStore, artifact_id: int, *, depth: int | None = None) -> ArtifactGraph:
    """The input closure of one artifact: everything that went into producing it.

    Answers "which raw inputs produced this number?" from what was actually read at
    compute time, not from what a recipe declares it might read. `depth=1` stops at the
    artifact's direct dependencies.

    Needs no recipe registry: it reads the recorded DAG.
    """
    ref = store.ref_by_id(artifact_id)
    if ref is None:
        raise MatviewError(f"no artifact with id {artifact_id}")
    ids = [artifact_id, *store.inputs_of([artifact_id], depth=depth)]
    return _assemble(store, ids, roots=(artifact_id,), direction="inputs")


def _assemble(
    store: GraphStore, ids: Sequence[int], *, roots: tuple[int, ...], direction: Direction
) -> ArtifactGraph:
    """Turn a walked id set into nodes and the edges that fall *inside* it.

    Edges to artifacts outside the set are dropped rather than pulling their nodes in,
    which is what makes a `depth`-bounded or `limit`-bounded walk a closed picture
    instead of one with dangling arrows.
    """
    nodes = {ref.id: ref for ref in store.refs_by_ids(ids)}
    edges: list[tuple[int, int]] = []
    for dependent, dep_edges in store.deps_of_many(list(nodes)).items():
        for edge in dep_edges:
            if edge.artifact_id in nodes:
                edges.append((dependent, edge.artifact_id))
    return ArtifactGraph(
        nodes=nodes,
        edges=tuple(sorted(set(edges))),
        roots=roots,
        direction=direction,
    )


# --- impact -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecipeCount:
    """How many artifacts of one recipe version are in some affected set."""

    recipe: str
    version: int
    artifacts: int
    #: How many of them are already stale, and so would not change state again.
    stale: int


@dataclass(frozen=True, slots=True)
class ImpactGroup:
    """One named set of affected artifacts within an impact answer."""

    label: str
    total: int
    by_recipe: tuple[RecipeCount, ...]
    #: Populated only when the caller asked to enumerate. A summary leaves it empty.
    refs: tuple[ArtifactRef, ...] = ()
    #: True when `limit` cut `refs` short. `total` is still the real figure.
    truncated: bool = False
    #: Every id in the group, kept so a caller can render the set as a graph.
    ids: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total": self.total,
            "truncated": self.truncated,
            "by_recipe": [
                {
                    "recipe": c.recipe,
                    "version": c.version,
                    "artifacts": c.artifacts,
                    "already_stale": c.stale,
                }
                for c in self.by_recipe
            ],
            "artifacts": [ref_to_dict(r) for r in self.refs],
        }


@dataclass(frozen=True, slots=True)
class ImpactReport:
    """What would happen, for one of the three questions impact answers.

    `notes` carries the prose. For a version bump that prose *is* the answer, and is the
    part of the fingerprint spec that confuses every new reader, so it is data rather
    than something the command-line layer prints on the side.
    """

    question: Literal["changed", "bump", "drop"]
    subject: str
    notes: tuple[str, ...]
    groups: tuple[ImpactGroup, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subject": self.subject,
            "notes": list(self.notes),
            "groups": [g.to_json() for g in self.groups],
        }

    def describe(self) -> str:
        lines = [f"{self.subject}", ""]
        lines.extend(self.notes)
        for group in self.groups:
            lines.append("")
            lines.append(f"{group.label}: {group.total}")
            for count in group.by_recipe:
                stale = f", {count.stale} already stale" if count.stale else ""
                lines.append(f"    {count.recipe} v{count.version}  {count.artifacts}{stale}")
            if not group.by_recipe:
                lines.append("    (none)")
            for ref in group.refs:
                lines.append(f"    {_label(ref)}")
            if group.truncated:
                lines.append(f"    ... ({group.total - len(group.refs)} more)")
        return "\n".join(lines)


def _group(
    store: GraphStore,
    label: str,
    ids: Sequence[int],
    *,
    enumerate_all: bool,
    limit: int | None,
) -> ImpactGroup:
    """Summarize an affected set, counting in SQL and materializing only on request.

    The default is a summary because it has to be: in the recorded 250k benchmark graph
    one shared root's impact is 255,000 artifacts, and listing those helps nobody.
    """
    ids = list(ids)
    counts = tuple(
        RecipeCount(
            recipe=str(row["name"]),
            version=int(row["version"]),
            artifacts=int(row["artifacts"]),
            stale=int(row["invalid"]),
        )
        for row in store.counts_by_recipe(ids)
    )
    refs: tuple[ArtifactRef, ...] = ()
    truncated = False
    if enumerate_all and ids:
        wanted = ids if limit is None else ids[:limit]
        refs = tuple(store.refs_by_ids(wanted))
        truncated = len(refs) < len(ids)
    return ImpactGroup(
        label=label,
        total=len(ids),
        by_recipe=counts,
        refs=refs,
        truncated=truncated,
        ids=tuple(ids),
    )


def impact_of_change(
    store: GraphStore,
    artifact_id: int,
    *,
    depth: int | None = None,
    enumerate_all: bool = False,
    limit: int | None = None,
) -> ImpactReport:
    """What would go stale if this artifact's bytes changed.

    This is the cascade the store already runs inside a write, asked as a question. The
    walk is the same one; only artifacts currently valid would actually change state,
    which the summary reports separately, because an already-stale dependent is not
    additional damage.
    """
    ref = store.ref_by_id(artifact_id)
    if ref is None:
        raise MatviewError(f"no artifact with id {artifact_id}")
    affected = store.dependents_of([artifact_id], depth=depth)
    group = _group(
        store,
        "would be marked stale",
        affected,
        enumerate_all=enumerate_all,
        limit=limit,
    )
    would_change = group.total - sum(c.stale for c in group.by_recipe)
    notes = (
        f"If these bytes changed, the cascade would reach {group.total} artifact(s).",
        f"{would_change} of them are valid today and would be marked stale; the rest already are.",
        "Nothing is deleted by an invalidation. The bytes stay, the valid flag clears, "
        "and a later read revalidates without recomputing if the fingerprint still "
        "matches.",
    )
    return ImpactReport(
        question="changed", subject=f"if {_label(ref)} changed", notes=notes, groups=(group,)
    )


def impact_of_bump(
    store: GraphStore,
    recipe: str,
    *,
    enumerate_all: bool = False,
    limit: int | None = None,
) -> ImpactReport:
    """What happens if this recipe's version is bumped.

    The honest answer is counter-intuitive and is most of this function's value:
    **nothing goes stale.** Under rule 8 a dependency's recipe version is not part of a
    dependent's fingerprint, so a bump on its own invalidates nothing anywhere.

    What actually happens is that the bump creates a new `recipe` row. The existing
    artifacts are still in the database, intact, but nothing resolves to them any more.
    On the next read each dependent re-resolves the dependency at the new version, finds
    it absent, recomputes it — and cascades only if the recomputed bytes differ from
    what the edge recorded. If the new version produces identical bytes, the whole graph
    above it stays valid and nothing is recomputed. That is early cutoff, and it is what
    rule 8 is for.
    """
    versions = sorted(
        {int(row["version"]) for row in store.recipe_rows() if str(row["name"]) == recipe}
    )
    if not versions:
        raise MatviewError(f"no recipe named {recipe!r} is stored in this database")
    current = versions[-1]

    unreachable = store.ids_of_recipe(recipe, current)
    superseded = [i for v in versions[:-1] for i in store.ids_of_recipe(recipe, v)]
    re_resolve = [i for i in store.dependents_of(unreachable, depth=1) if i not in set(unreachable)]

    notes = [
        f"Bumping {recipe} past v{current} marks NOTHING stale.",
        "",
        "A dependency's recipe version is not part of a dependent's fingerprint (rule 8 "
        "of the spec), so the bump alone invalidates nothing. What changes is "
        "reachability: the bump writes a new recipe row, and the artifacts below stay in "
        "the database, intact, with nothing resolving to them.",
        "",
        f"On the next read each of the dependents below re-resolves {recipe} at the new "
        "version, finds it absent, and recomputes it. Only then, and only if the "
        "recomputed bytes differ from the digest the edge recorded, does the cascade "
        "run. If the new version produces identical bytes, nothing above it is "
        "recomputed at all — that is early cutoff, and it is what rule 8 buys.",
        "",
        "To rebuild regardless of the bytes, use force_deps.",
    ]
    if superseded:
        notes.append(
            f"cache.gc() is what removes the {len(superseded)} artifact(s) earlier bumps "
            f"have already left behind, once nothing depends on them."
        )
    groups = (
        _group(
            store,
            f"become unreachable (currently v{current})",
            unreachable,
            enumerate_all=enumerate_all,
            limit=limit,
        ),
        _group(
            store,
            "re-resolve on their next read (direct dependents)",
            re_resolve,
            enumerate_all=enumerate_all,
            limit=limit,
        ),
        _group(
            store,
            "already unreachable from earlier bumps",
            superseded,
            enumerate_all=enumerate_all,
            limit=limit,
        ),
    )
    return ImpactReport(
        question="bump",
        subject=f"bump {recipe} (stored at v{current})",
        notes=tuple(notes),
        groups=groups,
    )


def impact_of_drop(
    store: GraphStore,
    recipe: str,
    *,
    version: int | None = None,
    enumerate_all: bool = False,
    limit: int | None = None,
) -> ImpactReport:
    """What `cache.drop()` would delete, and what it would mark stale.

    `drop` is deliberately not a command-line operation; being able to see what it would
    do is the useful half. Per D19 a drop deletes the recipe's artifacts outright — the
    one place in the library where bytes go away — and marks every transitive dependent
    stale, since their inputs no longer exist to revalidate against.
    """
    doomed = store.ids_of_recipe(recipe, version)
    if not doomed:
        where = recipe if version is None else f"{recipe} v{version}"
        raise MatviewError(f"no stored artifacts of {where}")
    invalidated = [i for i in store.dependents_of(doomed) if i not in set(doomed)]

    notes = (
        f"cache.drop({recipe!r}"
        + ("" if version is None else f", version={version}")
        + f") would delete {len(doomed)} artifact(s) and mark {len(invalidated)} stale.",
        "",
        "Deleting is what makes drop different from everything else here: invalidation "
        "keeps the bytes, and drop does not. The dependents keep theirs, but they cannot "
        "be revalidated without recomputing, because the inputs their fingerprints refer "
        "to are gone.",
        "",
        "Nothing has been modified by this command.",
    )
    groups = (
        _group(store, "deleted", doomed, enumerate_all=enumerate_all, limit=limit),
        _group(store, "marked stale", invalidated, enumerate_all=enumerate_all, limit=limit),
    )
    where = recipe if version is None else f"{recipe} v{version}"
    return ImpactReport(question="drop", subject=f"drop {where}", notes=notes, groups=groups)


def impact_graph(store: GraphStore, report: ImpactReport, root: int | None = None) -> ArtifactGraph:
    """Everything an impact report reaches, as one renderable graph.

    One graph over the union of the report's groups rather than one per group. A bump's
    unreachable artifacts and the dependents that will re-resolve them are joined by real
    edges, so drawing them apart would hide the only thing a drawing is good for — and
    two `graph LR` blocks are not a Mermaid document anyway. The per-group counts stay in
    the tree and JSON renderings, which is where a count belongs.
    """
    roots = () if root is None else (root,)
    seen: dict[int, None] = {}
    for group in report.groups:
        seen.update(dict.fromkeys(group.ids))
    seen.update(dict.fromkeys(roots))
    graph = _assemble(store, list(seen), roots=roots, direction="dependents")
    return ArtifactGraph(
        nodes=graph.nodes,
        edges=graph.edges,
        roots=graph.roots,
        direction=graph.direction,
        truncated=any(group.truncated for group in report.groups),
    )


# --- cost -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecipeCost:
    """One recipe version's share of what this database cost to build."""

    recipe: str
    version: int
    codec: str
    artifacts: int
    values: int
    compute_ms: int
    mean_compute_ms: float | None
    n_bytes: int


@dataclass(frozen=True, slots=True)
class PathStep:
    """One node on a critical path, with the running total up to and including it."""

    ref: ArtifactRef
    compute_ms: int
    cumulative_ms: int


@dataclass(frozen=True, slots=True)
class CostReport:
    """What one artifact embodies, and optionally where that cost is concentrated."""

    root: ArtifactRef
    #: Artifact *rows* in the closure, roots included.
    artifacts: int
    #: Distinct `(recipe, version, key_hash)` values, which is the unit cost is summed in.
    values: int
    compute_ms: int
    own_compute_ms: int | None
    #: Values in the closure with no recorded `compute_ms`, so the total understates.
    unmeasured_values: int
    n_bytes: int
    #: The `depth` the walk was bounded at, or None for the whole closure. Reported
    #: rather than a `truncated` flag, because whether a bound actually cut anything off
    #: cannot be known without doing the unbounded walk this bound exists to avoid.
    depth: int | None = None
    path: tuple[PathStep, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact": ref_to_dict(self.root),
            "artifacts": self.artifacts,
            "values": self.values,
            "compute_ms": self.compute_ms,
            "own_compute_ms": self.own_compute_ms,
            "unmeasured_values": self.unmeasured_values,
            "n_bytes": self.n_bytes,
            "depth": self.depth,
            "critical_path": [
                {
                    "artifact": ref_to_dict(step.ref),
                    "compute_ms": step.compute_ms,
                    "cumulative_ms": step.cumulative_ms,
                }
                for step in self.path
            ],
        }

    def describe(self) -> str:
        lines = [
            _label(self.root),
            "",
            f"  embodied compute   {self.compute_ms} ms",
            f"  of which its own   {self.own_compute_ms} ms",
            f"  closure            {self.values} value(s) in {self.artifacts} artifact(s)",
            f"  closure bytes      {self.n_bytes}",
        ]
        if self.unmeasured_values:
            lines.append(
                f"  unmeasured         {self.unmeasured_values} value(s) record no "
                f"compute_ms, so the total is a floor"
            )
        lines.append("  Cost is summed per value, not per artifact: every part of a partitioned")
        lines.append("  value records the cost of the one compute that produced them all.")
        if self.path:
            lines.append("")
            lines.append(
                f"  critical path: {self.path[-1].cumulative_ms} ms over "
                f"{len(self.path)} step(s), the artifact first and its deepest input last"
            )
            for step in self.path:
                lines.append(
                    f"    {step.compute_ms:>8} ms  {step.cumulative_ms:>9} cumulative  "
                    f"{_label(step.ref)}"
                )
        return "\n".join(lines)


def _closure(
    store: GraphStore, artifact_id: int, depth: int | None
) -> tuple[ArtifactRef, list[ArtifactRef]]:
    root = store.ref_by_id(artifact_id)
    if root is None:
        raise MatviewError(f"no artifact with id {artifact_id}")
    ids = [artifact_id, *store.inputs_of([artifact_id], depth=depth)]
    return root, store.refs_by_ids(ids)


def _value_costs(refs: Iterable[ArtifactRef]) -> dict[tuple[str, int, str], int | None]:
    """Collapse artifact rows to values, keeping each value's one recorded cost.

    **This is the deduplication the whole cost feature turns on.** `compute_ms` is
    recorded once per artifact, and a partitioned value is several artifacts that each
    carry the cost of the single compute that produced all of them. Summing the column
    over artifact rows multiplies such a value by its part count — confidently, silently,
    and with no symptom other than a wrong number. See DECISIONS.md D51.
    """
    costs: dict[tuple[str, int, str], int | None] = {}
    for ref in refs:
        value = _value_of(ref)
        if ref.compute_ms is None:
            costs.setdefault(value, None)
        else:
            costs[value] = ref.compute_ms
    return costs


def embodied_cost(
    store: GraphStore,
    artifact_id: int,
    *,
    depth: int | None = None,
    with_critical_path: bool = False,
) -> CostReport:
    """What this artifact and everything it was built from cost to compute.

    Summed over distinct values rather than artifact rows, so a partitioned dependency
    counts once. The value count is reported alongside the total precisely so that the
    deduplication is visible rather than something a reader has to take on trust.

    A value whose `compute_ms` was never recorded contributes nothing, and is counted
    under `unmeasured_values`: the total is a floor, not an estimate.
    """
    root, refs = _closure(store, artifact_id, depth)
    costs = _value_costs(refs)
    total = sum(ms for ms in costs.values() if ms is not None)
    path = critical_path(store, artifact_id, depth=depth) if with_critical_path else ()
    return CostReport(
        root=root,
        artifacts=len(refs),
        values=len(costs),
        compute_ms=total,
        own_compute_ms=root.compute_ms,
        unmeasured_values=sum(1 for ms in costs.values() if ms is None),
        n_bytes=sum(ref.n_bytes for ref in refs),
        depth=depth,
        path=path,
    )


def critical_path(
    store: GraphStore, artifact_id: int, *, depth: int | None = None
) -> tuple[PathStep, ...]:
    """The most expensive chain through an artifact's input closure.

    The chain, not just its length: the point of asking is to see which step dominates,
    and a number alone does not answer that.

    A topological walk in Python, which is the right shape here. Input closures are
    small — around fifty nodes for an aggregate in the recorded benchmark — and this is
    the input direction, not the dependent direction where one shared root reaches
    255,000 artifacts.

    No deduplication is needed along a path: a single chain passes through at most one
    part of any value, so each step's own `compute_ms` is that value's cost, counted
    once.
    """
    root_ref = store.ref_by_id(artifact_id)
    if root_ref is None:
        raise MatviewError(f"no artifact with id {artifact_id}")
    ids = [artifact_id, *store.inputs_of([artifact_id], depth=depth)]
    refs = {ref.id: ref for ref in store.refs_by_ids(ids)}
    edges = store.deps_of_many(list(refs))

    # Iterative rather than recursive, and with an explicit `open_` set: the stack keeps
    # a deep chain from exhausting the interpreter's, and refusing to descend into a node
    # already being expanded terminates on a cycle a corrupted database could contain
    # rather than looping. `best[node]` is the most expensive chain starting at `node`.
    best: dict[int, tuple[int, tuple[int, ...]]] = {}
    open_: set[int] = set()
    stack: list[tuple[int, bool]] = [(artifact_id, False)]
    while stack:
        node, expanded = stack.pop()
        if expanded:
            below = [
                best[e.artifact_id]
                for e in edges.get(node, ())
                if e.artifact_id in best and e.artifact_id != node
            ]
            own = refs[node].compute_ms or 0
            deeper = max(below, default=(0, ()))
            best[node] = (own + deeper[0], (node, *deeper[1]))
            open_.discard(node)
            continue
        if node in best or node in open_:
            continue
        open_.add(node)
        stack.append((node, True))
        for edge in edges.get(node, ()):
            if edge.artifact_id in refs and edge.artifact_id not in best:
                stack.append((edge.artifact_id, False))

    steps: list[PathStep] = []
    running = 0
    for node in best[artifact_id][1]:
        own = refs[node].compute_ms or 0
        running += own
        steps.append(PathStep(ref=refs[node], compute_ms=own, cumulative_ms=running))
    return tuple(steps)


def recipe_costs(store: GraphStore) -> tuple[RecipeCost, ...]:
    """Per recipe version: artifacts, distinct values, total and mean cost, and bytes.

    No graph walk at all — a `GROUP BY` over the artifact table — which makes it the
    cheapest question here and, for anyone about to optimize something, usually the
    first one worth asking.
    """
    return tuple(
        RecipeCost(
            recipe=str(row["name"]),
            version=int(row["version"]),
            codec=str(row["codec"]),
            artifacts=int(row["artifacts"]),
            values=int(row["n_values"]),
            compute_ms=int(row["compute_ms"]),
            mean_compute_ms=row["mean_compute_ms"],
            n_bytes=int(row["n_bytes"]),
        )
        for row in store.recipe_cost_rows()
    )
