"""SQLite-backed storage.

Payloads live entirely in the row. There is no spill path to an object store in v1 and
no `uri` column, so `data` is `NOT NULL`. `store()` enforces `max_artifact_bytes` and
raises when a payload exceeds it, rather than silently writing a blob large enough to
make SQLite unpleasant. Raising loudly is the point: it surfaces a codec or
partitioning problem at the moment it appears rather than as mysterious slowness months
later.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Any, Final, cast

from matview.canon import Scalar, index_value
from matview.errors import ArtifactTooLarge, StoreError
from matview.fingerprint import FINGERPRINT_SPEC_VERSION
from matview.types import Artifact, ArtifactRef, DepEdge, StoreResult

__all__ = ["SCHEMA_VERSION", "SqliteStore", "inspect_database"]

SCHEMA_VERSION: Final = 2

DEFAULT_MAX_ARTIFACT_BYTES: Final = 64 * 1024 * 1024

#: How long to wait for another writer before giving up. Duplicate computation by
#: concurrent runners is expected; failing because one of them held the file for a
#: moment is not.
DEFAULT_BUSY_TIMEOUT_MS: Final = 10_000

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  version     INTEGER NOT NULL,
  codec       TEXT NOT NULL,
  source_hash TEXT,
  UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS artifact (
  id           INTEGER PRIMARY KEY,
  recipe_id    INTEGER NOT NULL REFERENCES recipe(id),
  key_hash     TEXT NOT NULL,
  key_json     TEXT NOT NULL,
  part         TEXT,
  digest       TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  valid        INTEGER NOT NULL DEFAULT 1,
  n_bytes      INTEGER NOT NULL,
  data         BLOB NOT NULL,
  computed_at  INTEGER NOT NULL,
  refreshed_at INTEGER NOT NULL,
  compute_ms   INTEGER,
  UNIQUE (recipe_id, key_hash, part)
);

CREATE INDEX IF NOT EXISTS artifact_invalid ON artifact(valid) WHERE valid = 0;
CREATE INDEX IF NOT EXISTS artifact_recipe  ON artifact(recipe_id);

CREATE TABLE IF NOT EXISTS artifact_key (
  artifact_id INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  field       TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY (artifact_id, field)
);
CREATE INDEX IF NOT EXISTS artifact_key_lookup ON artifact_key(field, value);

CREATE TABLE IF NOT EXISTS dep (
  artifact_id     INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  dep_artifact_id INTEGER NOT NULL REFERENCES artifact(id),
  dep_digest      TEXT NOT NULL,
  PRIMARY KEY (artifact_id, dep_artifact_id)
);
CREATE INDEX IF NOT EXISTS dep_reverse ON dep(dep_artifact_id);

CREATE TABLE IF NOT EXISTS task (
  id           INTEGER PRIMARY KEY,
  recipe       TEXT NOT NULL,
  version      INTEGER NOT NULL,
  key_json     TEXT NOT NULL,
  part         TEXT,
  requested_at INTEGER NOT NULL,
  UNIQUE (recipe, version, key_json, part)
);
"""

_ARTIFACT_COLUMNS: Final = """
  a.id, r.name, r.version, r.codec, a.key_hash, a.key_json, a.part,
  a.digest, a.fingerprint, a.valid, a.n_bytes, a.computed_at, a.refreshed_at, a.compute_ms
"""


def _id_set_predicate(artifact_ids: Sequence[int], *, negated: bool = False) -> tuple[str, str]:
    """Render an ID membership predicate without one SQL variable per ID."""
    not_ = "NOT " if negated else ""
    return f"{not_}IN (SELECT value FROM json_each(?))", json.dumps(artifact_ids)


class SqliteStore:
    """One SQLite file. Connections are per-thread, with WAL on."""

    def __init__(
        self,
        path: str | PathLike[str],
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        read_only: bool = False,
    ) -> None:
        """Open a cache database, creating it if it does not exist.

        `read_only=True` opens it through SQLite's `mode=ro`, requires the file to
        already exist, and creates nothing — no schema, no WAL sidecar, no journal-mode
        change. An inspection tool needs that: a plain open creates a database on a
        mistyped path and reports it as empty, and switching someone's journal mode while
        they are diagnosing a problem is not a read. See DECISIONS.md D38.
        """
        self.path = str(path)
        self.max_artifact_bytes = max_artifact_bytes
        self.busy_timeout_ms = busy_timeout_ms
        self.read_only = read_only
        if read_only and not Path(self.path).exists():
            raise StoreError(f"{self.path} does not exist")
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._connect()

    # -- connection ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self.read_only:
            conn = sqlite3.connect(
                f"file:{urllib.parse.quote(self.path)}?mode=ro", uri=True, isolation_level=None
            )
        else:
            conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if not self.read_only:
            # Both of these write: the journal-mode switch to the file header, and
            # foreign_keys only matters where there are writes to constrain.
            self._enable_wal(conn)
            conn.execute("PRAGMA foreign_keys = ON")
        self._local.conn = conn
        with self._init_lock:
            if not self._initialized:
                if self.read_only:
                    self._check_versions()
                else:
                    self._create_schema(conn)
                self._initialized = True
        return conn

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """Switch the database to WAL, tolerating a concurrent opener.

        Two processes opening the same *new* file at the same moment both try to make
        this switch, and it needs a brief exclusive lock. `busy_timeout` does not cover
        it: SQLite does not invoke the busy handler for a journal-mode change, so the
        loser gets SQLITE_BUSY immediately however long the timeout is. That is not a
        hypothetical — it is what the first run of the cross-process test did, and the
        deployment the spec describes is exactly concurrent runners on one file.

        The mode is a persistent property of the file, so this costs nothing after the
        first successful switch: every later connection reads `wal` and returns.
        """
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            return

        deadline = time.monotonic() + self.busy_timeout_ms / 1000
        while True:
            try:
                # The pragma reports the mode it ended up in, and can decline to change
                # it without raising, so the return value is checked rather than the
                # absence of an exception.
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() == "wal":
                    return
            except sqlite3.OperationalError:
                pass
            if time.monotonic() >= deadline:
                raise StoreError(
                    f"{self.path}: could not switch to WAL within "
                    f"{self.busy_timeout_ms} ms; another connection is holding it."
                )
            time.sleep(0.01)

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        # executescript commits any open transaction before it runs, so the DDL cannot
        # share a transaction with the meta seeding. Every statement in it is IF NOT
        # EXISTS, which is what makes that safe.
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (k, v) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (k, v) VALUES (?, ?)",
                ("fingerprint_spec_version", str(FINGERPRINT_SPEC_VERSION)),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

        self._check_versions()

    def _check_versions(self) -> None:
        """Refuse a database this build cannot read correctly.

        Both of these would otherwise fail silently rather than loudly, which is the
        failure mode the whole design is arranged to avoid. A fingerprint spec mismatch
        makes every lookup miss and look like an ordinary cold cache; a schema mismatch
        means operating against a shape that is not the one in front of us.
        """
        try:
            self._connect().execute("SELECT 1 FROM json_each(?) LIMIT 1", ("[]",)).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(
                f"{self.path} requires SQLite JSON1; linked SQLite is "
                f"{sqlite3.sqlite_version}. Use SQLite 3.38 or newer."
            ) from exc

        recorded = self.meta("fingerprint_spec_version")
        if recorded != str(FINGERPRINT_SPEC_VERSION):
            raise StoreError(
                f"{self.path} was written under fingerprint_spec_version {recorded}, but this "
                f"build implements {FINGERPRINT_SPEC_VERSION}. Every hash in it would miss. "
                f"Use a different file."
            )

        schema = self.meta("schema_version")
        if schema == str(SCHEMA_VERSION):
            return
        try:
            found = int(str(schema))
        except (TypeError, ValueError):
            raise StoreError(
                f"{self.path} records schema_version {schema!r}, which is not a version "
                f"number. This is not a matview database, or it is damaged."
            ) from None
        if found > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by a newer build (schema_version {found}); this "
                f"one implements {SCHEMA_VERSION}. Upgrade matview rather than writing to it "
                f"with an older build."
            )
        raise StoreError(
            f"{self.path} was written under schema_version {found}; this build implements "
            f"{SCHEMA_VERSION}, and no migration exists. Keep the old file and start a new one."
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """One write, one transaction. A reader must never see an artifact whose dep
        edges have not landed, so everything a write touches happens in here."""
        if self.read_only:
            raise StoreError(f"{self.path} was opened read-only; this store cannot write")
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        try:
            yield cur
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def meta(self, key: str) -> str | None:
        row = self._connect().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return None if row is None else str(row["v"])

    # -- reads --------------------------------------------------------------

    def fetch(self, recipe: str, version: int, key_hash: str, part: str | None) -> Artifact | None:
        row = (
            self._connect()
            .execute(
                f"SELECT {_ARTIFACT_COLUMNS}, a.data FROM artifact a "
                "JOIN recipe r ON r.id = a.recipe_id "
                "WHERE r.name = ? AND r.version = ? AND a.key_hash = ? AND a.part IS ?",
                (recipe, version, key_hash, part),
            )
            .fetchone()
        )
        return None if row is None else _artifact_from_row(row)

    def fetch_all_parts(self, recipe: str, version: int, key_hash: str) -> list[Artifact]:
        rows = (
            self._connect()
            .execute(
                f"SELECT {_ARTIFACT_COLUMNS}, a.data FROM artifact a "
                "JOIN recipe r ON r.id = a.recipe_id "
                "WHERE r.name = ? AND r.version = ? AND a.key_hash = ? "
                "ORDER BY a.part IS NOT NULL, a.part",
                (recipe, version, key_hash),
            )
            .fetchall()
        )
        return [_artifact_from_row(row) for row in rows]

    def deps_of(self, artifact_id: int) -> list[DepEdge]:
        """The stored edges of one artifact: what it actually read, last time it ran."""
        rows = self._connect().execute(
            "SELECT r.name, r.version, a.key_hash, a.key_json, a.part, d.dep_digest, a.id "
            "FROM dep d JOIN artifact a ON a.id = d.dep_artifact_id "
            "JOIN recipe r ON r.id = a.recipe_id WHERE d.artifact_id = ?",
            (artifact_id,),
        )
        return [
            DepEdge(
                recipe=str(row["name"]),
                version=int(row["version"]),
                key_hash=str(row["key_hash"]),
                part=row["part"],
                digest=str(row["dep_digest"]),
                artifact_id=int(row["id"]),
                key_json=str(row["key_json"]),
            )
            for row in rows
        ]

    def dependents_of(self, artifact_ids: Sequence[int]) -> list[int]:
        """Everything that transitively read these artifacts. Walks `dep_reverse`."""
        if not artifact_ids:
            return []
        roots_predicate, roots_json = _id_set_predicate(artifact_ids)
        excluded_predicate, excluded_json = _id_set_predicate(artifact_ids, negated=True)
        rows = self._connect().execute(
            f"""
            WITH RECURSIVE affected(id) AS (
              SELECT id FROM artifact WHERE id {roots_predicate}
              UNION
              SELECT d.artifact_id FROM dep d JOIN affected a ON d.dep_artifact_id = a.id
            )
            SELECT id FROM affected WHERE id {excluded_predicate} ORDER BY id
            """,
            (roots_json, excluded_json),
        )
        return [int(row["id"]) for row in rows]

    def query(self, recipe: str | None = None, **indexed_fields: object) -> list[ArtifactRef]:
        joins: list[str] = []
        wheres: list[str] = []
        params: list[Any] = []
        for i, (field, value) in enumerate(sorted(indexed_fields.items())):
            joins.append(f"JOIN artifact_key k{i} ON k{i}.artifact_id = a.id")
            wheres.append(f"k{i}.field = ? AND k{i}.value = ?")
            params.extend((field, index_value(cast(Scalar, value))))
        if recipe is not None:
            wheres.append("r.name = ?")
            params.append(recipe)

        sql = [f"SELECT {_ARTIFACT_COLUMNS} FROM artifact a JOIN recipe r ON r.id = a.recipe_id"]
        sql.extend(joins)
        if wheres:
            sql.append("WHERE " + " AND ".join(wheres))
        sql.append("ORDER BY r.name, a.key_hash, a.part IS NOT NULL, a.part")
        rows = self._connect().execute(" ".join(sql), params).fetchall()
        return [_ref_from_row(row) for row in rows]

    def list_refs(
        self,
        recipe: str | None = None,
        *,
        version: int | None = None,
        part: str | None = None,
        valid: bool | None = None,
        fields: Mapping[str, str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ArtifactRef]:
        """List artifacts with the filters an inspection tool needs.

        Separate from `query()` rather than an extension of it: `query()` is the seam the
        spec fixes, and its `**indexed_fields` signature has no room for a `limit` or a
        `valid` filter without a caller's field name colliding with an option name.
        """
        joins: list[str] = []
        wheres: list[str] = []
        params: list[Any] = []
        for i, (field, value) in enumerate(sorted((fields or {}).items())):
            joins.append(f"JOIN artifact_key k{i} ON k{i}.artifact_id = a.id")
            wheres.append(f"k{i}.field = ? AND k{i}.value = ?")
            params.extend((field, value))
        if recipe is not None:
            wheres.append("r.name = ?")
            params.append(recipe)
        if version is not None:
            wheres.append("r.version = ?")
            params.append(version)
        if part is not None:
            wheres.append("a.part IS ?")
            params.append(part)
        if valid is not None:
            wheres.append("a.valid = ?")
            params.append(1 if valid else 0)

        sql = [f"SELECT {_ARTIFACT_COLUMNS} FROM artifact a JOIN recipe r ON r.id = a.recipe_id"]
        sql.extend(joins)
        if wheres:
            sql.append("WHERE " + " AND ".join(wheres))
        sql.append("ORDER BY r.name, r.version, a.key_json, a.part IS NOT NULL, a.part")
        if limit is not None:
            sql.append("LIMIT ? OFFSET ?")
            params.extend((limit, offset))
        return [_ref_from_row(row) for row in self._connect().execute(" ".join(sql), params)]

    def ref_by_id(self, artifact_id: int) -> ArtifactRef | None:
        """One artifact's metadata by row id, without needing to know its key."""
        row = (
            self._connect()
            .execute(
                f"SELECT {_ARTIFACT_COLUMNS} FROM artifact a "
                "JOIN recipe r ON r.id = a.recipe_id WHERE a.id = ?",
                (artifact_id,),
            )
            .fetchone()
        )
        return None if row is None else _ref_from_row(row)

    def fetch_by_id(self, artifact_id: int) -> Artifact | None:
        """One artifact including its payload, by row id."""
        row = (
            self._connect()
            .execute(
                f"SELECT {_ARTIFACT_COLUMNS}, a.data FROM artifact a "
                "JOIN recipe r ON r.id = a.recipe_id WHERE a.id = ?",
                (artifact_id,),
            )
            .fetchone()
        )
        return None if row is None else _artifact_from_row(row)

    def refs_by_key_hash(self, key_hash: str, recipe: str | None = None) -> list[ArtifactRef]:
        """Every artifact under one key hash, across recipes, versions and parts.

        A key hash is not unique on its own: the same parameters can be shared by several
        recipes, and a version bump leaves the old artifact in place.
        """
        sql = (
            f"SELECT {_ARTIFACT_COLUMNS} FROM artifact a "
            "JOIN recipe r ON r.id = a.recipe_id WHERE a.key_hash = ?"
        )
        params: list[Any] = [key_hash]
        if recipe is not None:
            sql += " AND r.name = ?"
            params.append(recipe)
        sql += " ORDER BY r.name, r.version, a.part IS NOT NULL, a.part"
        return [_ref_from_row(row) for row in self._connect().execute(sql, params)]

    def recipe_rows(self) -> list[dict[str, Any]]:
        """Every recipe version the database knows about, with its artifact count.

        Read from the `recipe` table, so it answers what is *stored* rather than what is
        registered in this process. A version present here with no registered recipe is
        exactly the situation the CLI exists to make visible.
        """
        rows = self._connect().execute(
            """
            SELECT r.name, r.version, r.codec, r.source_hash,
                   COUNT(a.id) AS artifacts,
                   COALESCE(SUM(a.n_bytes), 0) AS n_bytes,
                   COALESCE(SUM(a.valid = 0), 0) AS invalid
            FROM recipe r LEFT JOIN artifact a ON a.recipe_id = r.id
            GROUP BY r.id ORDER BY r.name, r.version
            """
        )
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        """Aggregates for the whole database, computed in SQL rather than in Python.

        At the scale the design cites — thousands of keys times dozens of indicators —
        materializing every row to count it is not an option.
        """
        conn = self._connect()
        totals = conn.execute(
            """
            SELECT COUNT(*) AS artifacts,
                   COALESCE(SUM(n_bytes), 0) AS n_bytes,
                   COALESCE(SUM(valid = 0), 0) AS invalid,
                   COALESCE(SUM(compute_ms), 0) AS compute_ms,
                   MIN(computed_at) AS oldest,
                   MAX(computed_at) AS newest,
                   MAX(n_bytes) AS largest
            FROM artifact
            """
        ).fetchone()
        return {
            "path": self.path,
            "schema_version": self.meta("schema_version"),
            "fingerprint_spec_version": self.meta("fingerprint_spec_version"),
            "artifacts": int(totals["artifacts"]),
            "invalid": int(totals["invalid"]),
            "n_bytes": int(totals["n_bytes"]),
            "largest_artifact_bytes": None if totals["largest"] is None else int(totals["largest"]),
            "compute_ms": int(totals["compute_ms"]),
            "oldest_computed_at": None if totals["oldest"] is None else int(totals["oldest"]),
            "newest_computed_at": None if totals["newest"] is None else int(totals["newest"]),
            "recipes": len({row["name"] for row in self.recipe_rows()}),
            "recipe_versions": len(self.recipe_rows()),
            "dep_edges": int(conn.execute("SELECT COUNT(*) FROM dep").fetchone()[0]),
            "pending_tasks": int(conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]),
        }

    def iter_payloads(
        self, recipe: str | None = None, batch: int = 200
    ) -> Iterator[tuple[int, str, bytes]]:
        """Yield `(artifact_id, digest, payload)` in batches.

        Batched rather than one query, because verifying a large cache must not hold
        every payload in memory at once.
        """
        conn = self._connect()
        sql = "SELECT a.id, a.digest, a.data FROM artifact a JOIN recipe r ON r.id = a.recipe_id"
        params: list[Any] = []
        if recipe is not None:
            sql += " WHERE r.name = ?"
            params.append(recipe)
        sql += " AND a.id > ?" if recipe is not None else " WHERE a.id > ?"
        sql += " ORDER BY a.id LIMIT ?"

        last_id = 0
        while True:
            rows = conn.execute(sql, (*params, last_id, batch)).fetchall()
            if not rows:
                return
            for row in rows:
                last_id = int(row["id"])
                yield int(row["id"]), str(row["digest"]), bytes(row["data"])

    def recipe_source_hash(self, recipe: str, version: int) -> str | None:
        """The advisory source hash last recorded for a recipe version, if any."""
        row = (
            self._connect()
            .execute(
                "SELECT source_hash FROM recipe WHERE name = ? AND version = ?", (recipe, version)
            )
            .fetchone()
        )
        return None if row is None or row["source_hash"] is None else str(row["source_hash"])

    def pending_tasks(self) -> list[sqlite3.Row]:
        """Rows written by `on_miss="enqueue"`. Nothing in v1 consumes them."""
        return list(self._connect().execute("SELECT * FROM task ORDER BY id").fetchall())

    # -- writes -------------------------------------------------------------

    def store(self, artifact: Artifact, deps: Sequence[DepEdge]) -> StoreResult:
        """Artifact, dep edges and cascade in one transaction, or none of them.

        The lookup is `part IS ?` rather than a reliance on the schema's
        UNIQUE (recipe_id, key_hash, part): SQLite treats NULLs as distinct in a UNIQUE
        index, so that constraint does not actually cover unpartitioned rows. Writers
        serialize on BEGIN IMMEDIATE, so select-then-write is safe across processes.
        See DECISIONS.md D23.
        """
        self._check_size(artifact)
        with self._transaction() as cur:
            recipe_id = self._recipe_id(cur, artifact)
            return self._store_one(cur, recipe_id, artifact, deps)

    def store_parts(
        self, artifacts: Sequence[Artifact], deps: Sequence[DepEdge]
    ) -> list[StoreResult]:
        """Every part of one value, in one transaction.

        A codec that returns a dict produces several artifacts from a single compute.
        They share a dep set and differ only in `part`, and a reader merging them must
        never see half of a rewrite, so they land together. This is the partitioned
        generalization of `store()`, which the spec fixes for the single-artifact case.
        See DECISIONS.md D24.

        Parts that this computation did *not* produce are removed. That is not
        invalidation, which never deletes: it is a part that no longer exists, and
        leaving it would let a whole-value read merge a phantom.
        """
        if not artifacts:
            return []
        first = artifacts[0]
        for artifact in artifacts:
            self._check_size(artifact)
            if (artifact.recipe, artifact.version, artifact.key_hash) != (
                first.recipe,
                first.version,
                first.key_hash,
            ):
                raise StoreError("store_parts writes parts of one value; these are not that")

        with self._transaction() as cur:
            recipe_id = self._recipe_id(cur, first)
            results = [self._store_one(cur, recipe_id, a, deps) for a in artifacts]
            self._prune_vanished_parts(cur, recipe_id, first.key_hash, [a.part for a in artifacts])
            return results

    def _check_size(self, artifact: Artifact) -> None:
        if artifact.n_bytes > self.max_artifact_bytes:
            raise ArtifactTooLarge(
                f"{artifact.recipe} produced {artifact.n_bytes} bytes for part "
                f"{artifact.part!r}, over the {self.max_artifact_bytes}-byte limit. "
                f"This is a codec or partitioning problem, not a limit to raise reflexively."
            )

    def _store_one(
        self, cur: sqlite3.Cursor, recipe_id: int, artifact: Artifact, deps: Sequence[DepEdge]
    ) -> StoreResult:
        row = cur.execute(
            "SELECT id, digest, fingerprint, valid FROM artifact "
            "WHERE recipe_id = ? AND key_hash = ? AND part IS ?",
            (recipe_id, artifact.key_hash, artifact.part),
        ).fetchone()

        if (
            row is not None
            and row["digest"] == artifact.digest
            and row["fingerprint"] == artifact.fingerprint
        ):
            return self._noop_write(
                cur, int(row["id"]), bool(row["valid"]), artifact.refreshed_at, deps
            )

        digest_changed = row is None or row["digest"] != artifact.digest

        if row is None:
            cur.execute(
                "INSERT INTO artifact (recipe_id, key_hash, key_json, part, digest, "
                "fingerprint, valid, n_bytes, data, computed_at, refreshed_at, compute_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    recipe_id,
                    artifact.key_hash,
                    artifact.key_json,
                    artifact.part,
                    artifact.digest,
                    artifact.fingerprint,
                    artifact.n_bytes,
                    artifact.data,
                    artifact.computed_at,
                    artifact.refreshed_at,
                    artifact.compute_ms,
                ),
            )
            artifact_id = int(cur.lastrowid or 0)
        else:
            artifact_id = int(row["id"])
            cur.execute(
                "UPDATE artifact SET key_json = ?, digest = ?, fingerprint = ?, valid = 1, "
                "n_bytes = ?, data = ?, computed_at = ?, refreshed_at = ?, compute_ms = ? "
                "WHERE id = ?",
                (
                    artifact.key_json,
                    artifact.digest,
                    artifact.fingerprint,
                    artifact.n_bytes,
                    artifact.data,
                    artifact.computed_at,
                    artifact.refreshed_at,
                    artifact.compute_ms,
                    artifact_id,
                ),
            )

        cur.execute("DELETE FROM artifact_key WHERE artifact_id = ?", (artifact_id,))
        for field, value in sorted(artifact.index_fields.items()):
            cur.execute(
                "INSERT INTO artifact_key (artifact_id, field, value) VALUES (?, ?, ?)",
                (artifact_id, field, value),
            )

        self._insert_dep_edges(cur, artifact_id, deps)

        invalidated = 0
        if digest_changed and row is not None:
            # A brand-new row cannot have dependents yet, so the cascade is only
            # meaningful when an existing artifact's content actually changed.
            invalidated = self._cascade(cur, artifact_id)

        return StoreResult(
            artifact_id=artifact_id,
            written=True,
            digest_changed=digest_changed,
            invalidated=invalidated,
        )

    def _prune_vanished_parts(
        self, cur: sqlite3.Cursor, recipe_id: int, key_hash: str, kept: Sequence[str | None]
    ) -> None:
        rows = cur.execute(
            "SELECT id, part FROM artifact WHERE recipe_id = ? AND key_hash = ?",
            (recipe_id, key_hash),
        ).fetchall()
        keep = set(kept)
        gone = [int(r["id"]) for r in rows if r["part"] not in keep]
        if not gone:
            return
        dependents = self.dependents_of(gone)
        if dependents:
            predicate, ids_json = _id_set_predicate(dependents)
            cur.execute(f"UPDATE artifact SET valid = 0 WHERE id {predicate}", (ids_json,))
        predicate, ids_json = _id_set_predicate(gone)
        cur.execute(f"DELETE FROM dep WHERE dep_artifact_id {predicate}", (ids_json,))
        cur.execute(f"DELETE FROM artifact WHERE id {predicate}", (ids_json,))

    def _noop_write(
        self,
        cur: sqlite3.Cursor,
        artifact_id: int,
        currently_valid: bool,
        refreshed_at: int,
        deps: Sequence[DepEdge],
    ) -> StoreResult:
        """Rule 4: a row whose digest and fingerprint already match is not rewritten.

        Redundant work from a duplicate runner must not rewrite a good row, because
        float nondeterminism across machines can yield equivalent-but-different bytes
        and flip every downstream fingerprint for no reason.

        Two corrections are still allowed here, and only these two. Neither writes the
        payload and neither cascades, which is what rule 4 protects. See DECISIONS.md D17.
        """
        cur.execute(
            "UPDATE artifact SET refreshed_at = ? WHERE id = ?", (refreshed_at, artifact_id)
        )
        if not currently_valid:
            # A forced recompute has just proven this row current. Leaving it stale
            # would be a lie the next reader has to re-derive.
            cur.execute("UPDATE artifact SET valid = 1 WHERE id = ?", (artifact_id,))

        # Because a dependency's recipe version is not in the fingerprint (rule 8), a
        # dependency version bump that produced identical bytes lands here with the
        # stored edge still pointing at the old version's row. Left alone, a later
        # write to the new row would not cascade to this one.
        stored = {
            int(r["dep_artifact_id"])
            for r in cur.execute(
                "SELECT dep_artifact_id FROM dep WHERE artifact_id = ?", (artifact_id,)
            )
        }
        if stored != {d.artifact_id for d in deps}:
            self._insert_dep_edges(cur, artifact_id, deps)

        return StoreResult(
            artifact_id=artifact_id, written=False, digest_changed=False, invalidated=0
        )

    def _insert_dep_edges(
        self, cur: sqlite3.Cursor, artifact_id: int, deps: Sequence[DepEdge]
    ) -> None:
        cur.execute("DELETE FROM dep WHERE artifact_id = ?", (artifact_id,))
        for dep in deps:
            cur.execute(
                "INSERT OR REPLACE INTO dep (artifact_id, dep_artifact_id, dep_digest) "
                "VALUES (?, ?, ?)",
                (artifact_id, dep.artifact_id, dep.digest),
            )

    def _cascade(self, cur: sqlite3.Cursor, changed_id: int) -> int:
        """Mark every transitive dependent stale. Never delete: clear the flag and keep
        the bytes, or early cutoff is lost permanently."""
        cur.execute(
            """
            WITH RECURSIVE affected(id) AS (
              SELECT :changed_id
              UNION
              SELECT d.artifact_id FROM dep d JOIN affected a ON d.dep_artifact_id = a.id
            )
            UPDATE artifact SET valid = 0
            WHERE id IN (SELECT id FROM affected WHERE id != :changed_id) AND valid = 1
            """,
            {"changed_id": changed_id},
        )
        # `AND valid = 1` is not in the spec's SQL. It changes nothing semantically,
        # since setting 0 where it is already 0 is a no-op, and it makes the count an
        # honest measure of what this write actually invalidated.
        #
        # sqlite3 reports rowcount as -1 for a statement that opens with WITH, so the
        # count comes from changes() instead.
        return int(cur.execute("SELECT changes()").fetchone()[0])

    def _recipe_id(self, cur: sqlite3.Cursor, artifact: Artifact) -> int:
        cur.execute(
            "INSERT INTO recipe (name, version, codec, source_hash) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (name, version) DO UPDATE SET codec = excluded.codec, "
            "source_hash = COALESCE(excluded.source_hash, recipe.source_hash)",
            (artifact.recipe, artifact.version, artifact.codec, artifact.source_hash),
        )
        row = cur.execute(
            "SELECT id FROM recipe WHERE name = ? AND version = ?",
            (artifact.recipe, artifact.version),
        ).fetchone()
        return int(row["id"])

    def invalidate(self, artifact_ids: Sequence[int]) -> int:
        """Mark these artifacts and everything downstream of them stale.

        Transitive on purpose: an artifact nobody can trust makes its dependents
        untrustworthy too, and a non-transitive public invalidate is a footgun.
        """
        if not artifact_ids:
            return 0
        ids = list(artifact_ids) + self.dependents_of(artifact_ids)
        predicate, ids_json = _id_set_predicate(ids)
        with self._transaction() as cur:
            cur.execute(
                f"UPDATE artifact SET valid = 0 WHERE id {predicate} AND valid = 1", (ids_json,)
            )
            return int(cur.rowcount)

    def revalidate(self, artifact_id: int) -> None:
        """The stale-but-current path: the fingerprint matched, so the bytes are good."""
        with self._transaction() as cur:
            cur.execute("UPDATE artifact SET valid = 1 WHERE id = ?", (artifact_id,))

    def drop(self, recipe: str, version: int | None = None) -> int:
        """Remove a recipe version's artifacts. See DECISIONS.md D19."""
        sql = "SELECT a.id FROM artifact a JOIN recipe r ON r.id = a.recipe_id WHERE r.name = ?"
        params: list[Any] = [recipe]
        if version is not None:
            sql += " AND r.version = ?"
            params.append(version)
        ids = [int(r["id"]) for r in self._connect().execute(sql, params)]
        if not ids:
            return 0

        dependents = [d for d in self.dependents_of(ids) if d not in set(ids)]
        ids_predicate, ids_json = _id_set_predicate(ids)
        with self._transaction() as cur:
            if dependents:
                dependents_predicate, dependents_json = _id_set_predicate(dependents)
                cur.execute(
                    f"UPDATE artifact SET valid = 0 WHERE id {dependents_predicate}",
                    (dependents_json,),
                )
            # dep.dep_artifact_id has no ON DELETE CASCADE, by design: an edge must not
            # vanish quietly. Dropping is the one place it is meant to, so it is explicit.
            cur.execute(f"DELETE FROM dep WHERE dep_artifact_id {ids_predicate}", (ids_json,))
            cur.execute(f"DELETE FROM artifact WHERE id {ids_predicate}", (ids_json,))
            return len(ids)

    def gc(self) -> int:
        """Drop superseded versions with no dependents.

        A version bump creates a *new* recipe row, and old artifacts remain in place,
        unreachable but intact. Dropping the version back finds them again. This is
        what actually removes them. Iterates to a fixed point, since removing one
        artifact can orphan another.
        """
        removed = 0
        while True:
            ids = [
                int(r["id"])
                for r in self._connect().execute(
                    """
                    SELECT a.id FROM artifact a
                    JOIN recipe r ON r.id = a.recipe_id
                    WHERE r.version < (SELECT MAX(r2.version) FROM recipe r2 WHERE r2.name = r.name)
                      AND a.id NOT IN (SELECT dep_artifact_id FROM dep)
                    """
                )
            ]
            if not ids:
                return removed
            predicate, ids_json = _id_set_predicate(ids)
            with self._transaction() as cur:
                cur.execute(f"DELETE FROM dep WHERE artifact_id {predicate}", (ids_json,))
                cur.execute(f"DELETE FROM artifact WHERE id {predicate}", (ids_json,))
            removed += len(ids)

    def enqueue(
        self, recipe: str, version: int, key_json: str, part: str | None, when: int
    ) -> None:
        """`on_miss="enqueue"` writes here. Nothing in v1 consumes it."""
        with self._transaction() as cur:
            # The schema's UNIQUE (recipe, version, key_json, part) does not constrain
            # rows with a NULL part: SQLite treats NULLs as distinct in a UNIQUE index,
            # so INSERT OR IGNORE would happily add a duplicate unpartitioned task. The
            # guard is explicit instead. See DECISIONS.md D23.
            cur.execute(
                "INSERT INTO task (recipe, version, key_json, part, requested_at) "
                "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS ("
                "  SELECT 1 FROM task WHERE recipe = ? AND version = ? AND key_json = ? "
                "  AND part IS ?"
                ")",
                (recipe, version, key_json, part, when, recipe, version, key_json, part),
            )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=int(row["id"]),
        recipe=str(row["name"]),
        version=int(row["version"]),
        codec=str(row["codec"]),
        key_hash=str(row["key_hash"]),
        key_json=str(row["key_json"]),
        part=row["part"],
        digest=str(row["digest"]),
        fingerprint=str(row["fingerprint"]),
        valid=bool(row["valid"]),
        n_bytes=int(row["n_bytes"]),
        data=bytes(row["data"]),
        computed_at=int(row["computed_at"]),
        refreshed_at=int(row["refreshed_at"]),
        compute_ms=None if row["compute_ms"] is None else int(row["compute_ms"]),
    )


def _ref_from_row(row: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        id=int(row["id"]),
        recipe=str(row["name"]),
        version=int(row["version"]),
        key_hash=str(row["key_hash"]),
        key_json=str(row["key_json"]),
        part=row["part"],
        digest=str(row["digest"]),
        fingerprint=str(row["fingerprint"]),
        valid=bool(row["valid"]),
        n_bytes=int(row["n_bytes"]),
        computed_at=int(row["computed_at"]),
        refreshed_at=int(row["refreshed_at"]),
        compute_ms=None if row["compute_ms"] is None else int(row["compute_ms"]),
    )


def inspect_database(path: str | PathLike[str]) -> dict[str, Any]:
    """Describe a matview database without opening it as a store.

    Deliberately version-blind: it reads only `meta`, the SQLite catalogue and row
    counts, none of which depend on the schema version. That is what lets it work on a
    file this build would otherwise refuse — which is exactly the moment someone reaches
    for a diagnostic tool. It reports what the file is and what wrote it; it does not
    pretend to read rows whose columns it does not know. See DECISIONS.md D36.

    Opens read-only, so it can never create or migrate the file it is inspecting.
    """
    target = Path(path)
    if not target.exists():
        raise StoreError(f"{target} does not exist")

    uri = f"file:{urllib.parse.quote(str(target))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = sorted(
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        meta: dict[str, str] = {}
        if "meta" in tables:
            meta = {str(row["k"]): str(row["v"]) for row in conn.execute("SELECT k, v FROM meta")}

        counts: dict[str, int] = {}
        columns: dict[str, list[str]] = {}
        for table in tables:
            # Table names come from the catalogue, not from user input.
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            columns[table] = [
                str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
            ]

        recorded_schema = meta.get("schema_version")
        recorded_spec = meta.get("fingerprint_spec_version")
        return {
            "path": str(target),
            "n_bytes": target.stat().st_size,
            "schema_version": recorded_schema,
            "fingerprint_spec_version": recorded_spec,
            "implemented_schema_version": SCHEMA_VERSION,
            "implemented_fingerprint_spec_version": FINGERPRINT_SPEC_VERSION,
            "readable": recorded_schema == str(SCHEMA_VERSION)
            and recorded_spec == str(FINGERPRINT_SPEC_VERSION),
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "tables": tables,
            "columns": columns,
            "row_counts": counts,
            "meta": meta,
        }
    finally:
        conn.close()
