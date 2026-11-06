"""Event ordering and timestamp-precision tests (Mission 23, Task 3C).

Context
-------
Persisted events are ordered by ``(created_at ASC, event_id ASC)``. ``event_id``
is a random UUID used purely as a deterministic tie-breaker, so any two events
that share a ``created_at`` are ordered arbitrarily. ``created_at`` is the
event's own ``timestamp``, generated from the OS wall clock, and that clock is
coarse on some platforms (on Windows a single tick is roughly 15 ms). Before
this task, back-to-back events therefore routinely received identical
timestamps and the observable order of a batch diverged from the order in which
the events were created.

The contract itself is deliberately unchanged: ordering is still chronological
with an ``event_id`` tie-break (see ``tests/test_event_history_api.py`` and the
live claim-ordering test in ``tests/test_outbox.py``). What changed is that
event timestamps are now guaranteed to be strictly increasing, so events
created in sequence never collapse onto the same ordering key.

These tests do not use sleeps; the clock is frozen explicitly where a coarse
clock needs to be simulated.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import pytest

from core.events import base as events_base
from core.events.events import InvestigationStarted, StageCompleted
from core.domain.state import InvestigationState
from core.persistence.postgres import PostgresRepository

from tests.test_postgres_persistence import PG_URL, POSTGRES_AVAILABLE

FROZEN_NOW = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _make_events(investigation_id: str, count: int) -> List:
    """Return ``count`` distinct domain events built back-to-back."""
    return [
        StageCompleted(investigation_id=investigation_id, stage=f"stage-{i}", total_tokens=i)
        for i in range(count)
    ]


def _timestamps(events: List) -> List[datetime]:
    return [datetime.fromisoformat(e.timestamp) for e in events]


# ---------------------------------------------------------------------------
# Offline: timestamp monotonicity
# ---------------------------------------------------------------------------


class TestEventTimestampMonotonicity:
    def test_sequential_creation_emits_strictly_increasing_timestamps(self):
        """Events created in a tight loop are never given the same timestamp.

        This is the core regression: it must hold regardless of how coarse the
        host clock is, so it is asserted rather than assumed.
        """
        events = _make_events("ord_monotonic", 200)
        ts = _timestamps(events)

        assert ts == sorted(ts)
        assert len(set(ts)) == len(ts)

    def test_frozen_clock_still_yields_strictly_increasing_timestamps(self, monkeypatch):
        """Simulate a completely stalled clock and require distinct timestamps.

        With the real OS clock this fails on Windows ~99.9% of the time, so the
        clock is frozen instead of relying on the host's resolution.
        """
        monkeypatch.setattr(events_base, "_wall_clock_now", lambda: FROZEN_NOW)
        monkeypatch.setattr(events_base, "_last_timestamp", None)

        events = _make_events("ord_frozen", 5)
        ts = _timestamps(events)

        assert len(set(ts)) == 5
        assert ts == [FROZEN_NOW + timedelta(microseconds=i) for i in range(5)]

    def test_creation_order_equals_canonical_order_under_a_frozen_clock(self, monkeypatch):
        """The canonical ``(created_at, event_id)`` order matches creation order.

        Guards the property that actually matters: a batch created in sequence
        must sort into that same sequence once persisted, including when the
        clock is too coarse to separate the events.
        """
        monkeypatch.setattr(events_base, "_wall_clock_now", lambda: FROZEN_NOW)
        monkeypatch.setattr(events_base, "_last_timestamp", None)

        events = _make_events("ord_canonical", 8)
        canonical = sorted(events, key=lambda e: (e.timestamp, e.event_id))

        assert [e.event_id for e in canonical] == [e.event_id for e in events]

    def test_concurrent_creation_still_yields_unique_timestamps(self):
        """The guard is thread-safe: concurrent creators never share a value."""
        created: List = []
        lock = threading.Lock()

        def _create(index: int) -> None:
            batch = _make_events(f"ord_thread_{index}", 25)
            with lock:
                created.extend(batch)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        ts = _timestamps(created)
        assert len(ts) == 200
        assert len(set(ts)) == len(ts)


# ---------------------------------------------------------------------------
# Offline: the documented tie-break contract is unchanged
# ---------------------------------------------------------------------------


class TestTieBreakContractUnchanged:
    def test_equal_timestamps_are_still_ordered_by_event_id(self):
        """Equal timestamps keep sorting by ``event_id``, as documented.

        The fix must not alter the ordering contract itself; only the chance of
        two events sharing a timestamp.
        """
        tied = FROZEN_NOW.isoformat()
        events = [
            StageCompleted(investigation_id="ord_tie", stage="c", total_tokens=1, timestamp=tied, event_id="evt_ccc"),
            StageCompleted(investigation_id="ord_tie", stage="a", total_tokens=1, timestamp=tied, event_id="evt_aaa"),
            StageCompleted(investigation_id="ord_tie", stage="b", total_tokens=1, timestamp=tied, event_id="evt_bbb"),
        ]

        canonical = sorted(events, key=lambda e: (e.timestamp, e.event_id))

        assert [e.event_id for e in canonical] == ["evt_aaa", "evt_bbb", "evt_ccc"]

    def test_outbox_created_at_equals_event_timestamp(self):
        """``created_at`` is the event timestamp; the invariant is preserved."""
        from core.persistence.outbox import OutboxEvent

        event = InvestigationStarted(investigation_id="ord_invariant", initial_stage="logs")
        assert OutboxEvent.from_domain_event(event).created_at == event.timestamp


# ---------------------------------------------------------------------------
# Live PostgreSQL: persistence, ordering and pagination
# ---------------------------------------------------------------------------


def _clean_live_records(inv_id: str) -> None:
    """Best-effort removal of live test rows for an investigation."""
    try:
        with PostgresRepository(PG_URL) as repo:
            with repo.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM investigations WHERE investigation_id = %s;", (inv_id,))
                        cur.execute("DELETE FROM event_outbox WHERE investigation_id = %s;", (inv_id,))
    except Exception:
        pass


def _paginate_all(repo: PostgresRepository, inv_id: str, page_size: int) -> List[str]:
    """Walk ``list_events`` page by page using the public keyset cursor."""
    collected: List[str] = []
    cursor: Optional[Tuple[str, str]] = None
    while True:
        page = repo.list_events(inv_id, limit=page_size, after_cursor=cursor)
        if not page:
            break
        collected.extend(e.event_id for e in page)
        last = page[-1]
        cursor = (last.created_at, last.event_id)
        if len(page) < page_size:
            break
    return collected


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Requires PostgreSQL connection at " + PG_URL)
class TestLiveEventOrdering:
    def test_atomic_batch_persists_in_creation_order(self, monkeypatch):
        """A batch saved under a stalled clock comes back in insertion order.

        Also re-checks that state and events are still persisted atomically.
        """
        inv_id = "ord_live_batch"
        _clean_live_records(inv_id)

        monkeypatch.setattr(events_base, "_wall_clock_now", lambda: FROZEN_NOW)
        monkeypatch.setattr(events_base, "_last_timestamp", None)

        try:
            state = InvestigationState(incident_id=inv_id)
            state.start_stage("logs")
            state.complete_stage("logs", output={"stage": "logs"})
            events = [
                InvestigationStarted(investigation_id=inv_id, initial_stage="logs"),
                StageCompleted(investigation_id=inv_id, stage="logs", total_tokens=1),
                StageCompleted(investigation_id=inv_id, stage="metrics", total_tokens=2),
            ]

            with PostgresRepository(PG_URL) as repo:
                repo.initialize_schema()
                repo.initialize_outbox_schema()
                repo.save(state, events=events)

                loaded = repo.load(inv_id)
                assert loaded is not None
                assert loaded.version == 1
                assert "logs" in loaded.stages

                expected = [e.event_id for e in events]
                for _ in range(3):
                    fresh = PostgresRepository(PG_URL)
                    try:
                        history = fresh.list_events(inv_id, limit=100)
                    finally:
                        fresh.close()
                    assert [e.event_id for e in history] == expected
        finally:
            _clean_live_records(inv_id)

    def test_list_events_orders_tied_timestamps_by_event_id_across_pages(self):
        """Tied timestamps page deterministically with no loss or duplication."""
        inv_id = "ord_live_tied"
        _clean_live_records(inv_id)
        tied_ts = datetime.now(timezone.utc)
        # Inserted deliberately out of event_id order to prove ORDER BY applies.
        inserted = ["evt_t3", "evt_t1", "evt_t5", "evt_t2", "evt_t4"]

        try:
            with PostgresRepository(PG_URL) as repo:
                repo.initialize_schema()
                repo.initialize_outbox_schema()
                with repo.pool.connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            for eid in inserted:
                                cur.execute(
                                    """
                                    INSERT INTO event_outbox (
                                        event_id, investigation_id, event_type, payload,
                                        status, created_at, retry_count
                                    ) VALUES (%s, %s, %s, %s, 'pending', %s, 0);
                                    """,
                                    (eid, inv_id, "InvestigationStarted", "{}", tied_ts),
                                )

                for page_size in (1, 2, 3):
                    assert _paginate_all(repo, inv_id, page_size) == sorted(inserted)
        finally:
            _clean_live_records(inv_id)

    def test_claim_pending_remains_ordered_for_tied_timestamps(self):
        """Delivery claiming keeps its documented canonical order."""
        inv_id = "ord_live_claim"
        _clean_live_records(inv_id)
        tied_ts = datetime.now(timezone.utc)
        inserted = ["evt_c2", "evt_c1", "evt_c3"]

        try:
            with PostgresRepository(PG_URL) as repo:
                repo.initialize_schema()
                repo.initialize_outbox_schema()
                with repo.pool.connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            for eid in inserted:
                                cur.execute(
                                    """
                                    INSERT INTO event_outbox (
                                        event_id, investigation_id, event_type, payload,
                                        status, created_at, retry_count
                                    ) VALUES (%s, %s, %s, %s, 'pending', %s, 0);
                                    """,
                                    (eid, inv_id, "InvestigationStarted", "{}", tied_ts),
                                )

                claimed = [
                    e for e in repo.claim_pending("order-worker", limit=1000)
                    if e.investigation_id == inv_id
                ]
                assert [e.event_id for e in claimed] == sorted(inserted)
        finally:
            _clean_live_records(inv_id)
