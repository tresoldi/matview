"""Stage 2: schema and SqliteStore."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from matview import SqliteStore
from matview.errors import ArtifactTooLarge, StoreError
from matview.fingerprint import FINGERPRINT_SPEC_VERSION
from matview.store import SCHEMA_VERSION
from matview.types import DepEdge

from .conftest import make_artifact


def _edge(artifact, artifact_id: int) -> DepEdge:
    return DepEdge(
        recipe=artifact.recipe,
        version=artifact.version,
        key_hash=artifact.key_hash,
        part=artifact.part,
        digest=artifact.digest,
        artifact_id=artifact_id,
    )


def _chain(store: SqliteStore, *, root_data: bytes = b'{"v":1}') -> tuple[int, int, int]:
    """root <- middle <- top, written the way the scheduler would write them."""
    root = make_artifact(recipe="root", data=root_data)
    root_id = store.store(root, ()).artifact_id

    middle = make_artifact(recipe="middle", data=b'{"v":2}', deps=(_edge(root, root_id),))
    middle_id = store.store(middle, (_edge(root, root_id),)).artifact_id

    top = make_artifact(recipe="top", data=b'{"v":3}', deps=(_edge(middle, middle_id),))
    top_id = store.store(top, (_edge(middle, middle_id),)).artifact_id
    return root_id, middle_id, top_id


def _many_ids(store: SqliteStore, *, recipe: str = "many", version: int = 1) -> list[int]:
    return [
        store.store(make_artifact(recipe=recipe, version=version, key={"n": n}), ()).artifact_id
        for n in range(11)
    ]


def _lower_variable_limit(store: SqliteStore) -> None:
    store._connect().setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)


# --- schema ----------------------------------------------------------------


def test_meta_records_both_versions(store: SqliteStore) -> None:
    assert store.meta("schema_version") == str(SCHEMA_VERSION)
    assert store.meta("fingerprint_spec_version") == str(FINGERPRINT_SPEC_VERSION)


def test_wal_and_foreign_keys_are_on(store: SqliteStore) -> None:
    conn = store._connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopening_an_existing_file_keeps_its_contents(tmp_path) -> None:
    path = tmp_path / "reopen.db"
    first = SqliteStore(path)
    artifact = make_artifact(recipe="r", data=b'{"a":1}')
    first.store(artifact, ())
    first.close()

    second = SqliteStore(path)
    assert second.fetch("r", 1, artifact.key_hash, None).data == b'{"a":1}'
    second.close()


def test_each_thread_gets_its_own_connection(store: SqliteStore) -> None:
    seen: list[int] = []

    def work() -> None:
        seen.append(id(store._connect()))

    main = id(store._connect())
    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert seen and seen[0] != main


@pytest.mark.parametrize("read_only", [False, True])
def test_open_refuses_a_sqlite_build_without_json1(
    tmp_path, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    path = tmp_path / "no-json1.db"
    SqliteStore(path).close()
    real_connect = sqlite3.connect

    def connect_without_json1(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(
            lambda action, _arg1, _arg2, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and _arg1 == "json_each"
                else sqlite3.SQLITE_OK
            )
        )
        return conn

    monkeypatch.setattr("matview.store.sqlite.sqlite3.connect", connect_without_json1)
    with pytest.raises(StoreError, match=r"SQLite .*JSON1"):
        SqliteStore(path, read_only=read_only)


# --- reads and writes ------------------------------------------------------


def test_fetch_of_an_absent_artifact_is_none(store: SqliteStore) -> None:
    assert store.fetch("nothing", 1, "0" * 64, None) is None
    assert store.fetch_all_parts("nothing", 1, "0" * 64) == []


def test_round_trip(store: SqliteStore) -> None:
    artifact = make_artifact(recipe="r", key={"symbol": "AAPL"}, data=b'{"a":1}')
    result = store.store(artifact, ())
    assert result.written is True
    assert result.digest_changed is True

    fetched = store.fetch("r", 1, artifact.key_hash, None)
    assert fetched.data == b'{"a":1}'
    assert fetched.digest == artifact.digest
    assert fetched.fingerprint == artifact.fingerprint
    assert fetched.valid is True
    assert fetched.key_json == '{"symbol":"AAPL"}'
    assert fetched.id == result.artifact_id


def test_parts_come_back_null_first_then_sorted(store: SqliteStore) -> None:
    for part in ("2026", None, "2024", "2025"):
        store.store(make_artifact(recipe="bars", part=part, data=f'{{"p":"{part}"}}'.encode()), ())
    parts = [a.part for a in store.fetch_all_parts("bars", 1, make_artifact().key_hash)]
    assert parts == [None, "2024", "2025", "2026"]


def test_max_artifact_bytes_raises_rather_than_writing(tmp_path) -> None:
    store = SqliteStore(tmp_path / "small.db", max_artifact_bytes=16)
    with pytest.raises(ArtifactTooLarge, match="codec or partitioning problem"):
        store.store(make_artifact(recipe="r", data=b"x" * 100), ())
    assert store.query(recipe="r") == []
    store.close()


# --- the cascade -----------------------------------------------------------


def test_a_digest_change_invalidates_transitively_and_keeps_the_bytes(store: SqliteStore) -> None:
    root_id, middle_id, top_id = _chain(store)
    assert all(ref.valid for ref in store.query())

    changed = make_artifact(recipe="root", data=b'{"v":999}')
    result = store.store(changed, ())
    assert result.artifact_id == root_id
    assert result.digest_changed is True
    assert result.invalidated == 2, "both levels above it"

    by_id = {ref.id: ref for ref in store.query()}
    assert by_id[root_id].valid is True, "the node that changed is current, not stale"
    assert by_id[middle_id].valid is False
    assert by_id[top_id].valid is False
    assert store.fetch("top", 1, changed.key_hash, None).data == b'{"v":3}', "bytes kept"


def test_an_unchanged_digest_does_not_cascade(store: SqliteStore) -> None:
    _root_id, middle_id, top_id = _chain(store)

    # Same bytes, different fingerprint: the artifact is rewritten but nothing downstream
    # can have been affected, because downstream keys on the digest alone.
    rewritten = make_artifact(recipe="root", data=b'{"v":1}', deps=())
    object.__setattr__(rewritten, "fingerprint", "f" * 64)
    result = store.store(rewritten, ())
    assert result.written is True
    assert result.digest_changed is False
    assert result.invalidated == 0

    by_id = {ref.id: ref for ref in store.query()}
    assert by_id[middle_id].valid is True
    assert by_id[top_id].valid is True


def test_the_noop_write_revalidates_a_stale_row(store: SqliteStore) -> None:
    """DECISIONS.md D17: a forced recompute that proves a stale row current must say so."""
    artifact = make_artifact(recipe="r", data=b'{"a":1}')
    artifact_id = store.store(artifact, ()).artifact_id
    store.invalidate([artifact_id])
    assert store.query(recipe="r")[0].valid is False

    result = store.store(make_artifact(recipe="r", data=b'{"a":1}'), ())
    assert result.written is False
    assert store.query(recipe="r")[0].valid is True


def test_the_noop_write_repoints_dep_edges(store: SqliteStore) -> None:
    """DECISIONS.md D17: the rule 8 hole. Same bytes under a new dependency version
    leaves the fingerprint unchanged, so the no-op path hits; the edge must still move,
    or a later write to the new row would never cascade here."""
    old_root = make_artifact(recipe="root", version=1, data=b'{"v":1}')
    old_id = store.store(old_root, ()).artifact_id
    leaf = make_artifact(recipe="leaf", data=b'{"v":2}', deps=(_edge(old_root, old_id),))
    leaf_id = store.store(leaf, (_edge(old_root, old_id),)).artifact_id

    new_root = make_artifact(recipe="root", version=2, data=b'{"v":1}')  # identical bytes
    new_id = store.store(new_root, ()).artifact_id
    assert new_id != old_id

    # The dependent recomputes to the same bytes and the same fingerprint.
    same_leaf = make_artifact(recipe="leaf", data=b'{"v":2}', deps=(_edge(new_root, new_id),))
    assert same_leaf.fingerprint == leaf.fingerprint, "rule 8: the version is not in there"
    result = store.store(same_leaf, (_edge(new_root, new_id),))
    assert result.written is False

    assert [d.artifact_id for d in store.deps_of(leaf_id)] == [new_id]
    assert store.dependents_of([new_id]) == [leaf_id]


# --- maintenance -----------------------------------------------------------


def test_dependents_of_is_transitive_and_excludes_the_roots(store: SqliteStore) -> None:
    root_id, middle_id, top_id = _chain(store)
    assert store.dependents_of([root_id]) == [middle_id, top_id]
    assert store.dependents_of([top_id]) == []
    assert store.dependents_of([]) == []


def test_dependents_of_works_below_sqlite_variable_limit(store: SqliteStore) -> None:
    ids = _many_ids(store)
    _lower_variable_limit(store)
    assert store.dependents_of(ids) == []


def test_invalidate_is_transitive_and_revalidate_is_not(store: SqliteStore) -> None:
    root_id, middle_id, _top_id = _chain(store)
    assert store.invalidate([root_id]) == 3
    assert not any(ref.valid for ref in store.query())

    store.revalidate(root_id)
    by_id = {ref.id: ref for ref in store.query()}
    assert by_id[root_id].valid is True
    assert by_id[middle_id].valid is False


def test_invalidate_works_below_sqlite_variable_limit(store: SqliteStore) -> None:
    ids = _many_ids(store)
    _lower_variable_limit(store)
    assert store.invalidate(ids) == len(ids)
    assert not any(ref.valid for ref in store.query(recipe="many"))


def test_drop_removes_a_version_and_marks_dependents_stale(store: SqliteStore) -> None:
    _root_id, middle_id, _top_id = _chain(store)
    assert store.drop("root") == 1

    assert store.query(recipe="root") == []
    assert store.query(recipe="middle")[0].valid is False, "its input is gone"
    assert store.deps_of(middle_id) == []
    assert store.drop("root") == 0


def test_drop_one_version_leaves_the_others(store: SqliteStore) -> None:
    store.store(make_artifact(recipe="r", version=1, data=b'{"v":1}'), ())
    store.store(make_artifact(recipe="r", version=2, data=b'{"v":2}'), ())
    assert store.drop("r", version=1) == 1
    assert [ref.version for ref in store.query(recipe="r")] == [2]


def test_drop_works_below_sqlite_variable_limit(store: SqliteStore) -> None:
    _many_ids(store)
    _lower_variable_limit(store)
    assert store.drop("many") == 11
    assert store.query(recipe="many") == []


def test_gc_drops_superseded_versions_with_no_dependents(store: SqliteStore) -> None:
    store.store(make_artifact(recipe="r", version=1, data=b'{"v":1}'), ())
    store.store(make_artifact(recipe="r", version=2, data=b'{"v":2}'), ())
    assert store.gc() == 1
    assert [ref.version for ref in store.query(recipe="r")] == [2]
    assert store.gc() == 0, "idempotent"


def test_gc_works_below_sqlite_variable_limit(store: SqliteStore) -> None:
    _many_ids(store, recipe="many", version=1)
    _many_ids(store, recipe="many", version=2)
    _lower_variable_limit(store)
    assert store.gc() == 11
    assert {ref.version for ref in store.query(recipe="many")} == {2}


def test_gc_keeps_a_superseded_version_that_something_still_depends_on(store: SqliteStore) -> None:
    old = make_artifact(recipe="root", version=1, data=b'{"v":1}')
    old_id = store.store(old, ()).artifact_id
    leaf = make_artifact(recipe="leaf", data=b'{"v":2}', deps=(_edge(old, old_id),))
    store.store(leaf, (_edge(old, old_id),))
    store.store(make_artifact(recipe="root", version=2, data=b'{"v":9}'), ())

    assert store.gc() == 0, "still referenced by an edge"
    store.drop("leaf")
    assert store.gc() == 1, "and now it is not"


def test_gc_iterates_to_a_fixed_point(store: SqliteStore) -> None:
    old_root = make_artifact(recipe="root", version=1, data=b'{"v":1}')
    old_root_id = store.store(old_root, ()).artifact_id
    old_leaf = make_artifact(
        recipe="leaf", version=1, data=b'{"v":2}', deps=(_edge(old_root, old_root_id),)
    )
    store.store(old_leaf, (_edge(old_root, old_root_id),))
    store.store(make_artifact(recipe="root", version=2, data=b'{"v":9}'), ())
    store.store(make_artifact(recipe="leaf", version=2, data=b'{"v":8}'), ())

    # Removing the superseded leaf orphans the superseded root, in one call.
    assert store.gc() == 2
    assert sorted(ref.version for ref in store.query()) == [2, 2]


def test_query_filters_on_promoted_index_fields(store: SqliteStore) -> None:
    store.store(
        make_artifact(recipe="bars", key={"symbol": "AAPL"}, index_fields={"symbol": "AAPL"}), ()
    )
    store.store(
        make_artifact(recipe="bars", key={"symbol": "MSFT"}, index_fields={"symbol": "MSFT"}), ()
    )
    assert len(store.query(recipe="bars")) == 2
    assert [r.key_json for r in store.query(symbol="AAPL")] == ['{"symbol":"AAPL"}']
    assert store.query(recipe="bars", symbol="NVDA") == []


def test_enqueue_is_idempotent(store: SqliteStore) -> None:
    store.enqueue("ohlcv", 1, '{"symbol":"AAPL"}', None, 1000)
    store.enqueue("ohlcv", 1, '{"symbol":"AAPL"}', None, 2000)
    tasks = store.pending_tasks()
    assert len(tasks) == 1
    assert tasks[0]["requested_at"] == 1000, "the first request wins; duplicates are ignored"


def test_a_second_unpartitioned_write_updates_rather_than_duplicating(store: SqliteStore) -> None:
    """The schema's UNIQUE does not cover NULL parts, so the write path has to.

    See DECISIONS.md D23. Without the `part IS ?` lookup this quietly grows a second row
    and every later read picks one of them arbitrarily.
    """
    store.store(make_artifact(recipe="r", data=b'{"v":1}'), ())
    store.store(make_artifact(recipe="r", data=b'{"v":2}'), ())
    refs = store.query(recipe="r")
    assert len(refs) == 1
    assert store.fetch("r", 1, refs[0].key_hash, None).data == b'{"v":2}'


def test_a_file_from_a_different_fingerprint_spec_is_refused(tmp_path) -> None:
    """A mismatch here would not error on its own: every lookup would simply miss."""
    path = tmp_path / "future.db"
    first = SqliteStore(path)
    first._connect().execute("UPDATE meta SET v = '99' WHERE k = 'fingerprint_spec_version'")
    first.close()

    with pytest.raises(Exception, match="fingerprint_spec_version 99"):
        SqliteStore(path)


def test_store_parts_refuses_artifacts_that_are_not_parts_of_one_value(store: SqliteStore) -> None:
    with pytest.raises(Exception, match="parts of one value"):
        store.store_parts(
            [
                make_artifact(recipe="a", part="2025", data=b"{}"),
                make_artifact(recipe="b", part="2026", data=b"{}"),
            ],
            (),
        )


def test_store_parts_of_nothing_is_nothing(store: SqliteStore) -> None:
    assert store.store_parts([], ()) == []


def test_pruning_parts_works_below_sqlite_variable_limit(store: SqliteStore) -> None:
    store.store_parts(
        [make_artifact(recipe="parts", part=str(n), data=str(n).encode()) for n in range(12)], ()
    )
    _lower_variable_limit(store)
    store.store_parts([make_artifact(recipe="parts", part="0", data=b"0")], ())
    assert [
        artifact.part for artifact in store.fetch_all_parts("parts", 1, make_artifact().key_hash)
    ] == ["0"]


def test_iter_payloads_walks_in_batches_without_offset(store: SqliteStore) -> None:
    ids = _many_ids(store)
    statements: list[str] = []
    store._connect().set_trace_callback(statements.append)
    try:
        found = [
            artifact_id for artifact_id, _digest, _data in store.iter_payloads("many", batch=2)
        ]
    finally:
        store._connect().set_trace_callback(None)
    assert found == ids
    assert all(" OFFSET " not in statement.upper() for statement in statements)


@pytest.mark.parametrize(
    ("recorded", "match"),
    [
        ("99", "newer build"),
        ("0", "no migration exists"),
        ("not-a-number", "not a version number"),
    ],
)
def test_a_file_from_a_different_schema_version_is_refused(tmp_path, recorded, match) -> None:
    """Written and never read, this check was missing entirely.

    A schema mismatch is the same class of hazard as a fingerprint spec mismatch: the
    file opens, the queries run, and what they mean is not what the caller thinks.
    """
    path = tmp_path / "other-schema.db"
    first = SqliteStore(path)
    first._connect().execute("UPDATE meta SET v = ? WHERE k = 'schema_version'", (recorded,))
    first.close()

    with pytest.raises(Exception, match=match):
        SqliteStore(path)
