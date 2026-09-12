"""Tests for PostgreSQL persistence repository (PostgresRepository).

Separated into:
1. Unit tests (Fast, Mocked/Offline — run without live PostgreSQL)
2. Integration tests (Run against live PostgreSQL when available; skipped gracefully otherwise)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.types.json import Jsonb

from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.persistence.factory import DEFAULT_PERSISTENCE_ROOT, get_repository
from core.persistence.filesystem import FilesystemRepository
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import (
    ConcurrencyError,
    PersistenceError,
    check_persistence_safety,
    validate_investigation_id,
)
from tests.test_persistence_contract import PersistenceContractTests

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db",
)


def _is_postgres_available(url: str) -> bool:
    """Check if live PostgreSQL database is reachable."""
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _is_postgres_available(PG_URL)


# ============================================================================
# UNIT TESTS (Offline / Mocked — Always Run)
# ============================================================================

class TestPostgresRepositoryUnit:
    """Unit tests for PostgresRepository logic without live database."""

    def test_constructor_validation(self):
        """Constructor validates argument types and rejects empty strings."""
        with pytest.raises(PersistenceError, match="cannot be empty"):
            PostgresRepository("")

        with pytest.raises(PersistenceError, match="cannot be empty"):
            PostgresRepository("   ")

        with pytest.raises(PersistenceError, match="Expected ConnectionPool or conninfo"):
            PostgresRepository(12345)  # type: ignore

    def test_from_env_validation(self, monkeypatch):
        """from_env() validates that environment variable is set."""
        monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
        with pytest.raises(PersistenceError, match="is not set or empty"):
            PostgresRepository.from_env()

        monkeypatch.setenv("SENTINEL_DATABASE_URL", "   ")
        with pytest.raises(PersistenceError, match="is not set or empty"):
            PostgresRepository.from_env()

    def test_save_requires_investigation_state(self):
        """save() rejects non-InvestigationState arguments."""
        # Create a mock pool
        mock_pool = MagicMock()
        repo = PostgresRepository(mock_pool)

        with pytest.raises(PersistenceError, match="accepts only InvestigationState"):
            repo.save({"incident_id": "test"})  # type: ignore

    def test_save_safety_check_ground_truth(self):
        """save() rejects states containing ground_truth.md references."""
        mock_pool = MagicMock()
        repo = PostgresRepository(mock_pool)

        state = InvestigationState(incident_id="inc_gt_leak")
        gt_token = "ground" + "_truth.md"
        state.metadata["forbidden"] = gt_token

        with pytest.raises(PersistenceError, match="forbidden benchmark artefact detected"):
            repo.save(state)

    def test_save_safety_check_secret_key(self):
        """save() rejects states containing API key references."""
        mock_pool = MagicMock()
        repo = PostgresRepository(mock_pool)

        state = InvestigationState(incident_id="inc_sec_leak")
        sec_token = "GROQ" + "_API_KEY"
        state.metadata["env"] = {sec_token: "gsk_12345"}

        with pytest.raises(PersistenceError, match="forbidden secret key detected"):
            repo.save(state)

    def test_invalid_id_rejected_on_all_methods(self):
        """Invalid IDs are rejected by load, exists, delete, and save."""
        mock_pool = MagicMock()
        repo = PostgresRepository(mock_pool)

        bad_ids = ["../traversal", "nested/id", ":colon", "has spaces", ""]
        for bad_id in bad_ids:
            with pytest.raises(PersistenceError):
                repo.load(bad_id)

            with pytest.raises(PersistenceError):
                repo.exists(bad_id)

            # delete with invalid ID is safe no-op
            repo.delete(bad_id)

    def test_occ_conflict_on_version_mismatch(self):
        """When database row version != state.version, ConcurrencyError is raised."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # DB has version 2, but state is version 1
        mock_cur.fetchone.return_value = (2,)

        repo = PostgresRepository(mock_pool)
        state = InvestigationState(incident_id="inc_occ_test", version=1)

        with pytest.raises(ConcurrencyError, match="Optimistic concurrency conflict"):
            repo.save(state)

    def test_occ_conflict_on_zero_rows_updated(self):
        """When UPDATE returns no row (concurrent modification), ConcurrencyError is raised."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # First query (SELECT FOR UPDATE) matches version 1
        # Second query (UPDATE RETURNING) returns None
        mock_cur.fetchone.side_effect = [(1,), None]

        repo = PostgresRepository(mock_pool)
        state = InvestigationState(incident_id="inc_occ_test", version=1)

        with pytest.raises(ConcurrencyError, match="Optimistic concurrency conflict"):
            repo.save(state)

    def test_occ_version_increment_synchronizes_state_payload(self):
        """Verify the OCC invariant on update:
        Before save: state.version = N, DB.version = N
        During save: UPDATE receives state_payload with version = N + 1
        After save: state.version = N + 1
        """
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # DB has version 1 for SELECT FOR UPDATE
        # UPDATE RETURNING returns 2
        mock_cur.fetchone.side_effect = [(1,), (2,)]

        repo = PostgresRepository(mock_pool)
        state = InvestigationState(incident_id="inc_version_sync", version=1)

        repo.save(state)

        # 1. State version in memory must now be 2
        assert state.version == 2

        # 2. Inspect the arguments passed to cur.execute for the UPDATE statement
        # call_args_list[0] is SELECT FOR UPDATE, call_args_list[1] is UPDATE
        assert mock_cur.execute.call_count == 2
        update_call = mock_cur.execute.call_args_list[1]
        sql, params = update_call[0]

        assert "UPDATE investigations" in sql
        # params index 6 is state_payload (Jsonb wrapper)
        jsonb_param = params[6]
        assert isinstance(jsonb_param, Jsonb)
        serialized_dict = jsonb_param.obj
        # Invariant check: state_payload MUST have version = N + 1 (2), NOT stale version 1!
        assert serialized_dict["version"] == 2

    def test_database_error_wrapped_in_persistence_error(self):
        """psycopg.Error is cleanly translated into PersistenceError."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_cur.execute.side_effect = psycopg.OperationalError("Connection dropped")

        repo = PostgresRepository(mock_pool)
        state = InvestigationState(incident_id="inc_db_err")

        with pytest.raises(PersistenceError, match="Database error during save"):
            repo.save(state)

    def test_load_malformed_state_payload(self):
        """Loading a record with invalid state payload raises PersistenceError."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Payload is not a valid dict
        mock_cur.fetchone.return_value = ("not a dict or json", 1)

        repo = PostgresRepository(mock_pool)
        with pytest.raises(PersistenceError, match="Malformed JSON|Invalid state format"):
            repo.load("inc_malformed")

    def test_load_incident_id_mismatch(self):
        """Loading a state whose payload incident_id differs from requested ID raises PersistenceError."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()

        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mismatched_payload = {"incident_id": "other_inc", "status": "RUNNING"}
        mock_cur.fetchone.return_value = (mismatched_payload, 1)

        repo = PostgresRepository(mock_pool)
        with pytest.raises(PersistenceError, match="Investigation ID mismatch"):
            repo.load("inc_requested")


# ============================================================================
# FACTORY TESTS (Offline / Always Run)
# ============================================================================

class TestRepositoryFactory:
    """Unit tests for get_repository() factory function."""

    def test_default_is_filesystem(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SENTINEL_PERSISTENCE_BACKEND", raising=False)
        monkeypatch.setenv("SENTINEL_PERSISTENCE_ROOT", str(tmp_path))

        repo = get_repository()
        assert isinstance(repo, FilesystemRepository)
        assert repo.persistence_root == tmp_path.resolve()

    def test_explicit_filesystem_backend(self, tmp_path):
        repo = get_repository(backend="filesystem", persistence_root=tmp_path)
        assert isinstance(repo, FilesystemRepository)
        assert repo.persistence_root == tmp_path.resolve()

    def test_postgres_missing_url_raises_error(self, monkeypatch):
        monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
        with pytest.raises(PersistenceError, match="SENTINEL_DATABASE_URL is not configured"):
            get_repository(backend="postgres")

    def test_postgres_explicit_url(self):
        dummy_url = "postgresql://user:pass@localhost:5432/test_db"
        repo = get_repository(backend="postgres", database_url=dummy_url)
        assert isinstance(repo, PostgresRepository)
        repo.close()

    def test_invalid_backend_raises_error(self):
        with pytest.raises(PersistenceError, match="Unsupported persistence backend"):
            get_repository(backend="redis_nosql")


# ============================================================================
# MIGRATION SCRIPT UNIT TESTS (Offline / Always Run)
# ============================================================================

class TestMigrationScriptUnit:
    """Unit tests for scripts/migrate_fs_to_postgres.py."""

    def test_migration_dry_run(self, tmp_path):
        from scripts.migrate_fs_to_postgres import migrate
        fs_repo = FilesystemRepository(persistence_root=tmp_path)
        state1 = InvestigationState(incident_id="mig_inc_01")
        state2 = InvestigationState(incident_id="mig_inc_02")
        fs_repo.save(state1)
        fs_repo.save(state2)

        mock_pg = MagicMock()
        succeeded, failed, errors = migrate(fs_repo=fs_repo, pg_repo=mock_pg, dry_run=True)

        assert succeeded == 2
        assert failed == 0
        assert errors == []
        mock_pg.save.assert_not_called()

    def test_migration_real_mocked_pg(self, tmp_path):
        from scripts.migrate_fs_to_postgres import migrate
        fs_repo = FilesystemRepository(persistence_root=tmp_path)
        state = InvestigationState(incident_id="mig_inc_mocked")
        fs_repo.save(state)

        mock_pg = MagicMock()
        mock_pg.load.return_value = state  # roundtrip matches

        succeeded, failed, errors = migrate(fs_repo=fs_repo, pg_repo=mock_pg, dry_run=False)

        assert succeeded == 1
        assert failed == 0
        assert errors == []
        mock_pg.save.assert_called_once()

    def test_migration_verification_mismatch(self, tmp_path):
        from scripts.migrate_fs_to_postgres import migrate
        fs_repo = FilesystemRepository(persistence_root=tmp_path)
        state = InvestigationState(incident_id="mig_inc_mismatch")
        fs_repo.save(state)

        mock_pg = MagicMock()
        different_state = InvestigationState(incident_id="mig_inc_mismatch", status=IncidentStatus.FAILED)
        mock_pg.load.return_value = different_state

        succeeded, failed, errors = migrate(fs_repo=fs_repo, pg_repo=mock_pg, dry_run=False)

        assert succeeded == 0
        assert failed == 1
        assert len(errors) == 1
        assert "dict comparison mismatch" in errors[0]


# ============================================================================
# INTEGRATION TESTS (Require Live PostgreSQL; skipped if not running)
# ============================================================================

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL)
class TestPostgresRepositoryIntegration(PersistenceContractTests):
    """Integration test suite executing against a real PostgreSQL instance."""

    @pytest.fixture(scope="class", autouse=True)
    def setup_database_schema(self):
        """Initialize schema on test database."""
        with PostgresRepository(PG_URL) as r:
            r.initialize_schema()

    @pytest.fixture
    def repo(self):
        """Provide a PostgresRepository connected to the test database and clean up after."""
        r = PostgresRepository(PG_URL)
        yield r
        # Clean up test records created during test
        try:
            with r.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM investigations WHERE investigation_id LIKE 'contract_%' OR investigation_id LIKE 'int_test_%';")
        except Exception:
            pass
        r.close()

    def test_real_occ_conflict_detection(self, repo: PostgresRepository):
        """Test genuine stale-state behaviour on real PostgreSQL:
        Worker A loads state
        Worker B loads state
        Worker A saves state (version -> 2)
        Worker B attempts save (version 1) -> raises ConcurrencyError
        """
        inv_id = "int_test_occ_conflict"
        state_a = InvestigationState(incident_id=inv_id)
        state_a.start_stage("logs")
        state_a.complete_stage("logs", output={"stage": "logs"})
        repo.save(state_a)

        # Worker A and Worker B both load the investigation
        loaded_a = repo.load(inv_id)
        loaded_b = repo.load(inv_id)

        assert loaded_a is not None
        assert loaded_b is not None
        assert loaded_a.version == 1
        assert loaded_b.version == 1

        # Worker A advances stage and saves
        loaded_a.start_stage("metrics")
        loaded_a.complete_stage("metrics", output={"stage": "metrics"})
        repo.save(loaded_a)
        assert loaded_a.version == 2

        # Worker B tries to save stale state
        loaded_b.start_stage("code")
        loaded_b.complete_stage("code", output={"stage": "code"})

        with pytest.raises(ConcurrencyError, match="Optimistic concurrency conflict"):
            repo.save(loaded_b)

        # Confirm DB state reflects Worker A's version
        fresh = repo.load(inv_id)
        assert fresh is not None
        assert fresh.version == 2
        assert "metrics" in fresh.stages
        assert "code" not in fresh.stages

    def test_real_transaction_rollback_on_error(self, repo: PostgresRepository):
        """Verify transaction rollback: partial failure leaves database state untouched."""
        inv_id = "int_test_rollback"
        state = InvestigationState(incident_id=inv_id)
        repo.save(state)

        # Intentionally inject a corrupt payload in save to simulate mid-transaction failure
        state.metadata["corrupt"] = object()  # non-serializable object
        with pytest.raises(PersistenceError):
            repo.save(state)

        # Verify original record remains intact at version 1
        loaded = repo.load(inv_id)
        assert loaded is not None
        assert loaded.version == 1

    def test_occ_version_invariant_real_database(self, repo: PostgresRepository):
        """Verify on real PostgreSQL:
        state.version = N+1, DB.version = N+1, state_payload.version = N+1
        """
        inv_id = "int_test_version_inv"
        state = InvestigationState(incident_id=inv_id)
        repo.save(state)
        assert state.version == 1

        # Check raw DB state
        with repo.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version, (state_payload->>'version')::int FROM investigations WHERE investigation_id = %s;",
                    (inv_id,),
                )
                db_ver, payload_ver = cur.fetchone()
                assert db_ver == 1
                assert payload_ver == 1

        # Update and save
        state.start_stage("logs")
        state.complete_stage("logs", output={"stage": "logs"})
        repo.save(state)
        assert state.version == 2

        # Check raw DB state again: DB.version and state_payload.version must both be 2
        with repo.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version, (state_payload->>'version')::int FROM investigations WHERE investigation_id = %s;",
                    (inv_id,),
                )
                db_ver, payload_ver = cur.fetchone()
                assert db_ver == 2
                assert payload_ver == 2

        # Load back: must reconstruct with version 2
        loaded = repo.load(inv_id)
        assert loaded is not None
        assert loaded.version == 2
