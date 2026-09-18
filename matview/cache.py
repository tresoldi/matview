"""The scheduler: a suspending scheduler with constructive traces.

Plain recursion. The walk carries an explicit stack of in-flight
`(recipe, key_hash, part)` triples; re-entry is a cycle and raises with the full path.
Graph depth in practice is single digits, so coroutines or trampolining would be
unnecessary complexity.

An artifact is in exactly one of three states, and conflating the first two is the
single most common way to get this wrong:

    absent   no row                      must compute
    stale    row exists, valid = 0       must verify; may or may not recompute
    valid    row exists, valid = 1       return it

Stale is not absent. Reading a stale artifact resolves its dependencies (usually
already valid, so this costs nothing), recomputes the fingerprint, and compares it to
the stored one. If they match, the flag flips back and the existing bytes are returned
without recomputing. That is early cutoff, and it is the entire payoff of digest-based
fingerprints over version-based ones.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from matview.canon import Scalar, utf16_sort_key
from matview.codecs import Codec, get_codec
from matview.context import Context
from matview.errors import CodecError, CycleError, MatviewError, MissingArtifacts
from matview.fingerprint import canonical_key_hash, digest_bytes, fingerprint
from matview.policy import Policy
from matview.registry import REGISTRY, Recipe, Registry
from matview.store.base import Store
from matview.types import (
    Artifact,
    ArtifactRef,
    DepEdge,
    MissingNode,
    OnMiss,
    PlanReport,
    stricter,
)

__all__ = ["Cache"]


class _Blocked(Exception):
    """Internal. A node could not be produced under the effective policy.

    Never escapes a top-level call: it is converted into a batched `MissingArtifacts`
    once the walk has collected everything it can reach.
    """


@dataclass
class _Walk:
    """State for one top-level call."""

    mode: Literal["compute", "plan"]
    policy: Policy
    stack: list[str] = field(default_factory=list)
    in_flight: set[tuple[str, str, str | None]] = field(default_factory=set)
    memo: dict[tuple[str, str, str | None], list[Artifact]] = field(default_factory=dict)
    missing: list[MissingNode] = field(default_factory=list)
    missing_seen: set[tuple[str, str, str | None]] = field(default_factory=set)
    stale: list[ArtifactRef] = field(default_factory=list)
    current: list[ArtifactRef] = field(default_factory=list)
    nondeterministic: set[str] = field(default_factory=set)


def _label(recipe: Recipe, key: Mapping[str, Scalar], part: str | None) -> str:
    params = ", ".join(f"{k}={v!r}" for k, v in sorted(key.items()))
    text = f"{recipe.name}(v{recipe.version})[{params}]"
    return text if part is None else f"{text} part={part!r}"


class Cache:
    """Ask for a value; get the stored one if it is current, and compute it if not.

    Not global, deliberately. The decorator registry being global will tempt an
    implementer into making this global too; it must not be.
    """

    def __init__(self, store: Store, *, registry: Registry | None = None) -> None:
        self.store = store
        self.registry = REGISTRY if registry is None else registry

    # -- public API ---------------------------------------------------------

    def get(
        self,
        recipe: str,
        *,
        part: str | None = None,
        on_miss: OnMiss = "compute",
        force: bool = False,
        force_deps: bool = False,
        deadline: float | None = None,
        **params: Any,
    ) -> Any:
        """Return the decoded value.

        The codec is a property of the recipe, so decoding is a lookup and never an
        inference.
        """
        data, codec = self._top_level(recipe, params, part, on_miss, force, force_deps, deadline)
        return codec.decode(data) if isinstance(data, bytes) else codec.decode_parts(data)

    def get_bytes(
        self,
        recipe: str,
        *,
        part: str | None = None,
        on_miss: OnMiss = "compute",
        force: bool = False,
        force_deps: bool = False,
        deadline: float | None = None,
        **params: Any,
    ) -> bytes | dict[str, bytes]:
        """The stored bytes.

        The escape hatch, and the one to use for replication and any future HTTP
        serving: decode-then-reencode both wastes work and risks failing to round-trip.
        """
        return self._top_level(recipe, params, part, on_miss, force, force_deps, deadline)[0]

    def plan(
        self,
        recipe: str,
        *,
        part: str | None = None,
        on_miss: OnMiss = "compute",
        force_deps: bool = False,
        **params: Any,
    ) -> PlanReport:
        """Walk the graph without computing anything.

        Absent and stale are reported separately, because stale work is often near-free
        while absent work at a protected root means you cannot proceed at all.

        The walk stops at an absent Python node, necessarily: its dependencies are only
        knowable by running it. A foreign recipe declares its inputs, so the walk
        continues through it — which is why the spec calls the declared graph the
        privileged case.
        """
        rec = self.registry.get(recipe)
        walk = _Walk(mode="plan", policy=Policy.from_call(on_miss=on_miss, force_deps=force_deps))
        key = rec.bind(params)
        with contextlib.suppress(_Blocked):
            self._resolve(walk, rec, key, part, force=False, policy=walk.policy, is_root=True)
        return PlanReport(
            absent=tuple(walk.missing),
            stale=tuple(walk.stale),
            valid=tuple(walk.current),
            nondeterministic=tuple(sorted(walk.nondeterministic)),
        )

    def query(self, recipe: str | None = None, **indexed_fields: Any) -> list[ArtifactRef]:
        return self.store.query(recipe, **indexed_fields)

    def drop(self, recipe: str, version: int | None = None) -> int:
        """Remove one recipe version's artifacts. See DECISIONS.md D19."""
        return self.store.drop(recipe, version)

    def gc(self) -> int:
        """Drop superseded versions with no dependents."""
        return self.store.gc()

    # -- walk ---------------------------------------------------------------

    def _top_level(
        self,
        recipe: str,
        params: dict[str, Any],
        part: str | None,
        on_miss: OnMiss,
        force: bool,
        force_deps: bool,
        deadline: float | None,
    ) -> tuple[bytes | dict[str, bytes], Codec]:
        rec = self.registry.get(recipe)
        policy = Policy.from_call(
            on_miss=on_miss, force=force, force_deps=force_deps, deadline=deadline
        )
        walk = _Walk(mode="compute", policy=policy)
        key = rec.bind(params)
        try:
            rows = self._resolve(walk, rec, key, part, force=force, policy=policy, is_root=True)
        except _Blocked:
            raise MissingArtifacts(walk.missing) from None
        if walk.missing:
            # Reached when a protected node was skipped but the value was still
            # obtainable. Report it anyway: the caller asked for something that could
            # not be fully honoured.
            raise MissingArtifacts(walk.missing)
        return self._payload(rows, part), get_codec(rec.codec)

    def _resolve_dependency(
        self,
        walk: _Walk,
        policy: Policy,
        recipe: str,
        params: dict[str, Any],
        part: str | None,
        on_miss: OnMiss | None,
    ) -> tuple[list[Artifact], Codec]:
        """What `ctx.get` calls.

        The policy handed down is the caller's, narrowed by any `on_miss` on this
        particular `ctx.get`. It is *not* the parent recipe's own declaration: a recipe
        declaring `on_miss="raise"` protects itself, not everything beneath it.
        """
        rec = self.registry.get(recipe)
        policy = policy.narrow(on_miss)
        key = rec.bind(params)
        rows = self._resolve(walk, rec, key, part, force=False, policy=policy)
        return rows, get_codec(rec.codec)

    def _resolve(
        self,
        walk: _Walk,
        rec: Recipe,
        key: Mapping[str, Scalar],
        part: str | None,
        *,
        force: bool,
        policy: Policy,
        is_root: bool = False,
    ) -> list[Artifact]:
        key_json, key_hash = canonical_key_hash(key)
        node = (rec.name, key_hash, part)
        label = _label(rec, key, part)

        if node in walk.in_flight:
            raise CycleError([*walk.stack, label])
        # Each descent checks the remaining budget before going any deeper.
        policy.check(label)

        if node in walk.memo:
            return walk.memo[node]

        if not rec.deterministic:
            walk.nondeterministic.add(rec.name)

        force_this = force or policy.force_deps
        rows = self._fetch(rec, key_hash, part)
        stale_and_wrong = False

        if rows and not force_this:
            if all(row.valid for row in rows):
                walk.current.extend(_as_refs(rows))
                if walk.mode == "plan":
                    self._walk_stored_deps(walk, rows, policy)
                return self._memoize(walk, node, rows)

            walk.stale.extend(_as_refs(row for row in rows if not row.valid))
            if self._verify(walk, rec, rows, policy):
                for row in rows:
                    if not row.valid:
                        self.store.revalidate(row.id or 0)
                rows = [replace(row, valid=True) for row in rows]
                return self._memoize(walk, node, rows)
            stale_and_wrong = True

        # Everything below here needs the value produced.
        #
        # The ceiling protects a recipe "everywhere it appears as a transitive
        # dependency", which is exactly where the danger is: a backtester asking for
        # sharpe with on_miss="compute" must not fire a paid vendor request six levels
        # down that it never mentioned. A caller naming the recipe *directly* is the
        # opposite case, and the spec's own ingest example depends on it working:
        # `cache.get("ohlcv", symbol=..., on_miss="compute")` reads as deliberate
        # intent. So the recipe's declaration is a ceiling below the root, and the
        # caller's ask governs at it. See DECISIONS.md D27.
        effective = policy.on_miss if is_root else stricter(policy.on_miss, rec.on_miss)

        if walk.mode == "plan":
            if not rows:
                self._record_missing(walk, rec, key, part, effective)
                raise _Blocked
            return self._memoize(walk, node, rows)

        if effective != "compute":
            if rows and not stale_and_wrong:
                # A forced recompute of a protected node is skipped, not an error: the
                # stored value is still there and still current. The ceiling applies
                # under force_deps too, which is exactly what "protected roots are
                # skipped even then" means.
                return self._memoize(walk, node, rows)
            if effective == "enqueue":
                self.store.enqueue(rec.name, rec.version, key_json, part, int(time.time()))
            self._record_missing(walk, rec, key, part, effective)
            raise _Blocked

        rows = self._compute(walk, rec, key, key_json, key_hash, part, policy)
        if part is not None:
            rows = [row for row in rows if row.part == part]
            if not rows:
                raise CodecError(
                    f"{rec.name} was asked for part {part!r} but its codec produced no such part"
                )
        return self._memoize(walk, node, rows)

    def _fetch(self, rec: Recipe, key_hash: str, part: str | None) -> list[Artifact]:
        rows = self.store.fetch_all_parts(rec.name, rec.version, key_hash)
        if part is None:
            return rows
        return [row for row in rows if row.part == part]

    def _compute(
        self,
        walk: _Walk,
        rec: Recipe,
        key: Mapping[str, Scalar],
        key_json: str,
        key_hash: str,
        part: str | None,
        policy: Policy,
    ) -> list[Artifact]:
        node = (rec.name, key_hash, part)
        label = _label(rec, key, part)
        collected: list[DepEdge] = []
        ctx = Context(self, walk, policy, collected)

        walk.stack.append(label)
        walk.in_flight.add(node)
        started = time.monotonic()
        try:
            if rec.fn is None:
                raise MatviewError(f"{rec.name} has no Python implementation and no foreign runner")
            value = rec.fn(ctx, **key)
        finally:
            walk.stack.pop()
            walk.in_flight.discard(node)
        # The recipe body's own cost, with dependency resolution attributed to the
        # dependency rather than to this node. See DECISIONS.md D6.
        elapsed = time.monotonic() - started - ctx.dep_seconds

        # A failed compute raised above and is never cached, in any form.
        return self._persist(rec, key, key_json, key_hash, value, collected, elapsed)

    def _persist(
        self,
        rec: Recipe,
        key: Mapping[str, Scalar],
        key_json: str,
        key_hash: str,
        value: Any,
        deps: Sequence[DepEdge],
        elapsed: float,
    ) -> list[Artifact]:
        codec = get_codec(rec.codec)
        encoded = codec.encode(value)

        if isinstance(encoded, dict):
            if not encoded:
                raise CodecError(
                    f"codec {rec.codec!r} split {rec.name}'s value into zero parts, which "
                    f"would store nothing at all"
                )
            payloads: list[tuple[str | None, bytes]] = sorted(
                ((str(k), v) for k, v in encoded.items()), key=lambda kv: utf16_sort_key(kv[0])
            )
        else:
            payloads = [(None, encoded)]

        now = int(time.time())
        compute_ms = max(0, round(elapsed * 1000))
        index_fields = rec.index_fields(key)

        artifacts = [
            Artifact(
                recipe=rec.name,
                version=rec.version,
                codec=rec.codec,
                key_hash=key_hash,
                key_json=key_json,
                part=label,
                digest=digest_bytes(data),
                fingerprint=fingerprint(
                    recipe=rec.name,
                    version=rec.version,
                    key_hash=key_hash,
                    part=label,
                    deps=deps,
                ),
                n_bytes=len(data),
                data=data,
                computed_at=now,
                # Every part of one value shares the cost of producing that value;
                # the compute happened once.
                compute_ms=compute_ms,
                source_hash=rec.source_hash,
                index_fields=index_fields,
            )
            for label, data in payloads
        ]

        results = self.store.store_parts(artifacts, deps)
        return [replace(a, id=r.artifact_id) for a, r in zip(artifacts, results, strict=True)]

    # -- stale verification -------------------------------------------------

    def _verify(self, walk: _Walk, rec: Recipe, rows: Sequence[Artifact], policy: Policy) -> bool:
        """Recompute the fingerprint from the stored edges and compare.

        Dependencies are re-resolved at their *current* registered version, which may be
        a different artifact row than the one the edge points at. That is what makes
        early cutoff work across a dependency version bump: if the new version produces
        identical bytes, the digest is unchanged, so this fingerprint matches and
        nothing is recomputed.

        Every edge is resolved even after one of them is blocked, so a cold run collects
        all of its protected misses in one pass rather than one per attempt.
        """
        current = True
        for row in rows:
            edges: list[DepEdge] = []
            blocked = False
            for edge in self.store.deps_of(row.id or 0):
                try:
                    dep = self.registry.get(edge.recipe)
                    key = json.loads(edge.key_json) if edge.key_json else {}
                    resolved = self._resolve(walk, dep, key, edge.part, force=False, policy=policy)
                except _Blocked:
                    blocked = True
                    continue
                edges.extend(self._edges_for(resolved))
            if blocked:
                current = False
                continue
            recomputed = fingerprint(
                recipe=rec.name,
                version=rec.version,
                key_hash=row.key_hash,
                part=row.part,
                deps=edges,
            )
            if recomputed != row.fingerprint:
                current = False
        return current

    def _walk_stored_deps(self, walk: _Walk, rows: Sequence[Artifact], policy: Policy) -> None:
        """Plan mode: follow what a node read last time, to report the whole subgraph."""
        for row in rows:
            for edge in self.store.deps_of(row.id or 0):
                try:
                    dep = self.registry.get(edge.recipe)
                    key = json.loads(edge.key_json) if edge.key_json else {}
                    self._resolve(walk, dep, key, edge.part, force=False, policy=policy)
                except _Blocked:
                    continue

    # -- helpers ------------------------------------------------------------

    def _edges_for(self, rows: Sequence[Artifact]) -> list[DepEdge]:
        return [
            DepEdge(
                recipe=row.recipe,
                version=row.version,
                key_hash=row.key_hash,
                part=row.part,
                digest=row.digest,
                artifact_id=row.id or 0,
                key_json=row.key_json,
            )
            for row in rows
        ]

    def _payload(self, rows: Sequence[Artifact], part: str | None) -> bytes | dict[str, bytes]:
        if part is not None or (len(rows) == 1 and rows[0].part is None):
            return rows[0].data
        return {str(row.part): row.data for row in rows}

    def _memoize(
        self, walk: _Walk, node: tuple[str, str, str | None], rows: list[Artifact]
    ) -> list[Artifact]:
        """One node resolves once per top-level call.

        A diamond in the graph would otherwise recompute the shared node twice, and the
        two results could differ for a non-deterministic recipe. See DECISIONS.md D25.
        """
        walk.memo[node] = rows
        return rows

    def _record_missing(
        self,
        walk: _Walk,
        rec: Recipe,
        key: Mapping[str, Scalar],
        part: str | None,
        action: OnMiss,
    ) -> None:
        """Collect rather than raise.

        Walking as much of the graph as possible and reporting every protected node at
        once turns the protection from an obstacle into a planning tool: a cold run
        tells you in one pass exactly what needs ingesting.
        """
        node = (rec.name, canonical_key_hash(key)[1], part)
        if node in walk.missing_seen:
            return
        walk.missing_seen.add(node)
        walk.missing.append(
            MissingNode(
                recipe=rec.name, version=rec.version, key=dict(key), part=part, action=action
            )
        )


def _as_refs(rows: Any) -> list[ArtifactRef]:
    return [
        ArtifactRef(
            id=row.id or 0,
            recipe=row.recipe,
            version=row.version,
            key_hash=row.key_hash,
            key_json=row.key_json,
            part=row.part,
            digest=row.digest,
            fingerprint=row.fingerprint,
            valid=row.valid,
            n_bytes=row.n_bytes,
            computed_at=row.computed_at,
            compute_ms=row.compute_ms,
        )
        for row in rows
    ]
