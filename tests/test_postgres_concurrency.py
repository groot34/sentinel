"""Live PostgreSQL concurrency and integration tests for Sentinel 2.0 (Mission 14).

Validates:
1. Genuine multi-worker Optimistic Concurrency Control (OCC) conflicts under concurrent writes.
2. Concurrent lease claims on expired investigations (exactly one winner, loser rejected).
3. Concurrent asynchronous resume HTTP requests (one 202 Accepted, competitor 409 Conflict).
4. Transactional outbox atomicity and clean rollback on mid-transaction persistence failures.

Deterministic coordination is achieved via threading.Barrier primitives across
separate database connection pools and client contexts. All tests skip gracefully
when PostgreSQL is unavailable.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.events.events import InvestigationStarted, StageCompleted
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import ConcurrencyError, PersistenceError

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db",
)


def _is_postgres_available(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _is_postgres_available(PG_URL)


@pytest.fixture(autouse=True)
def clean_active_registry():
    """Ensure process-local active investigation slot registry is clean before and after."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def incidents_dir(tmp_path: Path) -> Path:
    """Create a temporary incidents root with a valid incident bundle."""
    inc_root = tmp_path / "incidents"
    inc_root.mkdir()
    (inc_root / "inc_conc_test").mkdir()
    return inc_root


def _clean_test_records(investigation_id: str):
    """Clean up test records from investigations and event_outbox tables."""
    try:
        with psycopg.connect(PG_URL, connect_timeout=2) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM investigations WHERE investigation_id = %s;", (investigation_id,))
                    cur.execute("DELETE FROM event_outbox WHERE investigation_id = %s;", (investigation_id,))
    except Exception:
        pass


# ============================================================================
# LIVE POSTGRESQL CONCURRENCY & INTEGRATION TESTS
# ============================================================================


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Live PostgreSQL not available at " + PG_URL)
class TestPostgresLiveConcurrency:
    """Live concurrency test suite executing against a running PostgreSQL instance."""

    @pytest.fixture(scope="class", autouse=True)
    def bootstrap_schema(self):
        """Ensure schema is initialized prior to running live tests."""
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()

    def test_live_occ_concurrent_competing_writers(self):
        """A. OCC Conflict: Two concurrent workers load version 1 of an investigation.
        Both coordinate via a barrier and attempt to commit updates concurrently.
        Assert that exactly one succeeds (version 2) and the other raises ConcurrencyError.
        Confirm the stored state and version reflect only the winning update.
        """
        inv_id = "conc_occ_competing_writers"
        _clean_test_records(inv_id)

        # 1. Setup initial state in PostgreSQL at version 1
        with PostgresRepository(PG_URL) as setup_repo:
            initial_state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            initial_state.start_stage("logs")
            initial_state.complete_stage("logs", output={"stage": "logs", "result": "initial"})
            setup_repo.save(initial_state)
            assert initial_state.version == 1

        barrier = threading.Barrier(2)
        results: Dict[str, Any] = {"worker_1": None, "worker_2": None}
        errors: Dict[str, Any] = {"worker_1": None, "worker_2": None}

        def worker_task(worker_name: str, stage_result: str):
            repo = PostgresRepository(PG_URL)
            try:
                # Both workers load version 1
                state = repo.load(inv_id)
                assert state is not None
                assert state.version == 1

                # Advance state locally
                state.start_stage("metrics")
                state.complete_stage("metrics", output={"stage": "metrics", "winner": worker_name})

                # Synchronize workers at the barrier right before save
                barrier.wait(timeout=5.0)

                # Attempt competing write
                repo.save(state)
                results[worker_name] = state.version
            except ConcurrencyError as ce:
                errors[worker_name] = ce
            except Exception as ex:
                errors[worker_name] = ex
            finally:
                repo.close()

        t1 = threading.Thread(target=worker_task, args=("worker_1", "output_1"))
        t2 = threading.Thread(target=worker_task, args=("worker_2", "output_2"))

        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert not t1.is_alive(), "Worker 1 thread timed out"
        assert not t2.is_alive(), "Worker 2 thread timed out"

        # Assert exactly one winner and one ConcurrencyError
        successes = [k for k, v in results.items() if v == 2]
        conflicts = [k for k, v in errors.items() if isinstance(v, ConcurrencyError)]

        assert len(successes) == 1, f"Expected exactly 1 successful write, got: {results}"
        assert len(conflicts) == 1, f"Expected exactly 1 ConcurrencyError, got: {errors}"
        assert "Optimistic concurrency conflict" in str(errors[conflicts[0]])

        # Verify PostgreSQL database state matches the winner
        with PostgresRepository(PG_URL) as verify_repo:
            final_state = verify_repo.load(inv_id)
            assert final_state is not None
            assert final_state.version == 2
            winning_worker = successes[0]
            assert final_state.stages["metrics"].output["winner"] == winning_worker

        _clean_test_records(inv_id)

    def test_live_concurrent_expired_lease_claims(self):
        """B. Competing Lease Claims: Create an investigation with an expired lease.
        Two independent workers attempt to acquire ownership concurrently via barrier.
        Assert that exactly one worker acquires ownership and saves version 2, while
        the competing worker fails with ConcurrencyError and cannot overwrite the lease.
        """
        inv_id = "conc_expired_lease_race"
        _clean_test_records(inv_id)

        # 1. Setup initial state with an EXPIRED lease
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with PostgresRepository(PG_URL) as setup_repo:
            state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            state.lease_owner = "crashed_worker_99"
            state.lease_expires_at = expired_time
            setup_repo.save(state)
            assert state.version == 1

        barrier = threading.Barrier(2)
        winners: List[str] = []
        losers: List[str] = []

        def claim_task(owner_id: str):
            repo = PostgresRepository(PG_URL)
            try:
                # Load expired state
                st = repo.load(inv_id)
                assert st is not None
                assert st.version == 1
                assert st.is_lease_active() is False

                # Claim lease locally
                acquired = st.acquire_lease(owner_id=owner_id, ttl_seconds=300)
                assert acquired is True

                # Synchronize right before database OCC commit
                barrier.wait(timeout=5.0)

                repo.save(st)
                winners.append(owner_id)
            except ConcurrencyError:
                losers.append(owner_id)
            finally:
                repo.close()

        t1 = threading.Thread(target=claim_task, args=("worker_alpha",))
        t2 = threading.Thread(target=claim_task, args=("worker_beta",))

        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert not t1.is_alive()
        assert not t2.is_alive()

        assert len(winners) == 1, f"Expected exactly 1 lease winner, got: {winners}"
        assert len(losers) == 1, f"Expected exactly 1 lease loser, got: {losers}"

        # Verify database record reflects winner only
        with PostgresRepository(PG_URL) as verify_repo:
            persisted = verify_repo.load(inv_id)
            assert persisted is not None
            assert persisted.version == 2
            assert persisted.lease_owner == winners[0]
            assert persisted.is_lease_active() is True
            assert persisted.lease_owner != losers[0]

        _clean_test_records(inv_id)

    def test_live_concurrent_async_resume_requests(self, incidents_dir: Path):
        """C. Concurrent Async Resume: Exercises the actual Starlette API route backed by
        PostgresRepository. Two competing async resume requests hit POST /investigations/{id}/resume
        concurrently with Prefer: respond-async.
        Assert that exactly one request receives 202 Accepted and the other receives 409 Conflict.
        """
        inv_id = "inc_conc_test"
        _clean_test_records(inv_id)

        try:
            # 1. Setup investigation in PostgreSQL eligible for resume (FAILED, no active lease)
            with PostgresRepository(PG_URL) as setup_repo:
                initial = InvestigationState(incident_id=inv_id, status=IncidentStatus.FAILED)
                initial.lease_owner = None
                initial.lease_expires_at = None
                setup_repo.save(initial)
                assert initial.version == 1

            barrier = threading.Barrier(2)
            responses: Dict[str, Any] = {}

            def send_async_resume(client_id: str):
                # Create a separate TestClient for each client
                repo = PostgresRepository(PG_URL)
                app = create_app(repository=repo, incidents_root=incidents_dir)
                client = TestClient(app)
                try:
                    barrier.wait(timeout=5.0)

                    resp = client.post(
                        f"/investigations/{inv_id}/resume",
                        headers={"Prefer": "respond-async"},
                    )
                    responses[client_id] = resp
                finally:
                    repo.close()

            # Patch the orchestrator once, in a shared context that stays active for
            # BOTH thread starts and joins. Patching it per-thread races on the
            # process-global symbol: whichever thread exits its `with` block first
            # un-patches api.routes.IncidentOrchestrator while the other thread's
            # synchronous background task may still construct the real orchestrator.
            with patch("api.routes.IncidentOrchestrator") as mock_orch:
                instance = MagicMock()
                instance.resume.return_value = {
                    "incident_id": inv_id,
                    "pipeline_status": "COMPLETED",
                    "diagnoses": [],
                }
                instance.state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
                mock_orch.return_value = instance

                t1 = threading.Thread(target=send_async_resume, args=("client_1",))
                t2 = threading.Thread(target=send_async_resume, args=("client_2",))

                t1.start()
                t2.start()
                t1.join(timeout=10.0)
                t2.join(timeout=10.0)

            assert not t1.is_alive()
            assert not t2.is_alive()

            status_codes = [r.status_code for r in responses.values()]
            assert 202 in status_codes, f"Expected a 202 Accepted, got: {status_codes}"
            assert 409 in status_codes, f"Expected a 409 Conflict, got: {status_codes}"

            accepted_resp = [r for r in responses.values() if r.status_code == 202][0]
            conflict_resp = [r for r in responses.values() if r.status_code == 409][0]

            assert accepted_resp.json()["status"] == "RUNNING"
            assert accepted_resp.headers.get("preference-applied") == "respond-async"
            assert conflict_resp.json()["error"] == "conflict"

            # Verify PostgreSQL database state: version incremented, active lease acquired
            with PostgresRepository(PG_URL) as verify_repo:
                final_st = verify_repo.load(inv_id)
                assert final_st is not None
                assert final_st.version == 2
                assert final_st.status == IncidentStatus.RUNNING
                assert final_st.is_lease_active() is True
                assert final_st.lease_owner is not None
        finally:
            _clean_test_records(inv_id)

    def test_live_transactional_outbox_atomicity_and_rollback(self):
        """D. Transactional Outbox: Test real PostgreSQL transaction boundary.
        1. State updates and outbox events commit together atomically.
        2. A simulated database error inside the transaction rolls back both the state update
           and the event insertion, leaving no orphaned events or state changes.
        """
        inv_id = "conc_outbox_atomic"
        _clean_test_records(inv_id)

        with PostgresRepository(PG_URL) as repo:
            # 1. Successful atomic commit of state + events
            state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            state.start_stage("logs")
            state.complete_stage("logs", output={"stage": "logs"})
            event1 = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            event2 = StageCompleted(investigation_id=inv_id, stage="logs", total_tokens=150)

            repo.save(state, events=[event1, event2])
            assert state.version == 1

            # Verify both state and outbox events exist in database
            loaded = repo.load(inv_id)
            assert loaded is not None
            assert loaded.version == 1

            pending = repo.fetch_pending()
            matching_events = [e for e in pending if e.investigation_id == inv_id]
            assert len(matching_events) == 2
            event_ids = {e.event_id for e in matching_events}
            assert event1.event_id in event_ids
            assert event2.event_id in event_ids

            # 2. Forced failure inside transaction: attempt save with invalid duplicate event
            # that violates primary key in event_outbox, or simulated SQL error
            loaded.start_stage("metrics")
            loaded.complete_stage("metrics", output={"stage": "metrics"})

            # Inject a failure by providing an event with invalid un-serializable payload
            # or monkey-patching psycopg cursor to raise an exception during event insertion
            original_execute = psycopg.Cursor.execute

            def failing_execute(self_cur, query, params=None):
                if "INSERT INTO event_outbox" in str(query):
                    raise psycopg.OperationalError("Simulated network/disk crash during outbox insert")
                return original_execute(self_cur, query, params)

            event3 = StageCompleted(investigation_id=inv_id, stage="metrics", total_tokens=200)

            with patch.object(psycopg.Cursor, "execute", failing_execute):
                with pytest.raises(PersistenceError, match="Simulated network/disk crash"):
                    repo.save(loaded, events=[event3])

            # 3. Assert complete rollback: state version remains 1, stage 'metrics' is NOT in DB,
            # and event3 is NOT in event_outbox
            rollback_state = repo.load(inv_id)
            assert rollback_state is not None
            assert rollback_state.version == 1
            assert "metrics" not in rollback_state.stages

            rechecked_events = repo.fetch_pending()
            matching_rechecked = [e for e in rechecked_events if e.investigation_id == inv_id]
            # Still exactly 2 events; event3 was rolled back cleanly
            assert len(matching_rechecked) == 2
            assert event3.event_id not in {e.event_id for e in matching_rechecked}

        _clean_test_records(inv_id)
