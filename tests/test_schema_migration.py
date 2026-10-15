"""Tests for PostgreSQL authoritative schema migration and verification engine.

Divided into:
1. Unit Tests (Mocked / Offline) — tests discovery, ordering, tracking, rollback,
   existing-schema adoption, and verification logic without requiring live PostgreSQL.
2. Integration Tests (Live PostgreSQL) — executed when PostgreSQL is active at
   SENTINEL_DATABASE_URL; skipped gracefully when offline.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from core.persistence.postgres import PostgresRepository
from core.persistence.schema import (
    DEFAULT_SQL_DIR,
    EXPECTED_TABLES,
    SchemaMigration,
    SchemaMigrationError,
    SchemaVerificationError,
    SchemaVerificationReport,
    acquire_connection,
    adopt_existing_schema_if_needed,
    apply_migrations,
    discover_migrations,
    ensure_migration_table,
    get_applied_versions,
    get_existing_tables,
    get_table_columns,
    get_table_indexes,
    verify_schema,
)

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


# ============================================================================
# 1. UNIT TESTS (Offline / Mocked — Always Run)
# ============================================================================


class TestSchemaDiscoveryUnit:
    """Tests for migration discovery, file scanning, and ordering."""

    def test_discover_migrations_order(self):
        """Migrations are discovered in strict alphabetical/numeric order."""
        migrations = discover_migrations(DEFAULT_SQL_DIR)
        assert len(migrations) >= 2

        versions = [m.version for m in migrations]
        assert versions[0] == "001_initial_schema"
        assert versions[1] == "002_outbox_schema"

        for m in migrations:
            assert m.filename.endswith(".sql")
            assert len(m.checksum) == 64  # SHA-256 hex length
            assert m.path.is_file()

    def test_discover_migrations_custom_directory(self, tmp_path: Path):
        """Migrations in custom directory are discovered and ordered."""
        (tmp_path / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
        (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "not_sql.txt").write_text("ignored", encoding="utf-8")

        migrations = discover_migrations(tmp_path)
        assert len(migrations) == 2
        assert migrations[0].version == "001_first"
        assert migrations[1].version == "002_second"

    def test_discover_migrations_missing_directory(self, tmp_path: Path):
        """Raises SchemaMigrationError if directory does not exist."""
        non_existent = tmp_path / "does_not_exist"
        with pytest.raises(SchemaMigrationError, match="does not exist"):
            discover_migrations(non_existent)


class TestAcquireConnectionUnit:
    """Tests for connection acquisition abstraction."""

    def test_acquire_connection_rejects_empty_string(self):
        """Rejects empty connection string with SchemaMigrationError."""
        with pytest.raises(SchemaMigrationError, match="cannot be empty"):
            with acquire_connection("   "):
                pass

    def test_acquire_connection_rejects_unsupported_type(self):
        """Rejects unexpected type with SchemaMigrationError."""
        with pytest.raises(SchemaMigrationError, match="Unsupported connection target type"):
            with acquire_connection(12345):  # type: ignore
                pass

    def test_acquire_connection_accepts_existing_connection(self):
        """Yields existing psycopg.Connection directly."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        with acquire_connection(mock_conn) as conn:
            assert conn is mock_conn


class TestSchemaMigrationRunnerUnit:
    """Unit tests for migration execution, repeat execution, and rollback."""

    def test_apply_migrations_fresh_database(self, tmp_path: Path):
        """Fresh database applies migrations sequentially and records them in tracking table."""
        (tmp_path / "001_init.sql").write_text("CREATE TABLE test_one (id INT);", encoding="utf-8")
        (tmp_path / "002_next.sql").write_text("CREATE TABLE test_two (id INT);", encoding="utf-8")

        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Simulate empty tracking table and empty database
        mock_cur.fetchall.return_value = []

        applied = apply_migrations(mock_conn, sql_dir=tmp_path)

        assert applied == ["001_init", "002_next"]
        # Verify schema_migrations table was created
        create_calls = [
            c for c in mock_cur.execute.call_args_list
            if "CREATE TABLE IF NOT EXISTS schema_migrations" in str(c)
        ]
        assert len(create_calls) >= 1

    def test_apply_migrations_repeat_execution_is_noop(self, tmp_path: Path):
        """Re-running migrations when already applied performs no new executions."""
        sql_file = tmp_path / "001_init.sql"
        sql_file.write_text("CREATE TABLE test_one (id INT);", encoding="utf-8")
        import hashlib
        checksum = hashlib.sha256(sql_file.read_bytes()).hexdigest()

        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Simulate 001_init is recorded in schema_migrations with matching checksum
        mock_cur.fetchall.side_effect = [
            [],  # get_existing_tables during adoption
            [("001_init",)],  # get_applied_versions in adopt_existing_schema_if_needed
            [("001_init", checksum)],  # get_applied_migrations in apply_migrations
        ]

        applied = apply_migrations(mock_conn, sql_dir=tmp_path)
        assert applied == []

    def test_checksum_mismatch_raises_error(self, tmp_path: Path):
        """Detects if an already-applied migration file on disk has been tampered with or modified."""
        sql_file = tmp_path / "001_init.sql"
        sql_file.write_text("CREATE TABLE test_one (id INT);", encoding="utf-8")

        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Simulate recorded checksum in database differs from current file checksum
        stale_checksum = "a" * 64
        mock_cur.fetchall.side_effect = [
            [],  # get_existing_tables during adoption
            [("001_init",)],  # get_applied_versions in adopt_existing_schema_if_needed
            [("001_init", stale_checksum)],  # get_applied_migrations in apply_migrations
        ]

        with pytest.raises(SchemaMigrationError, match="Checksum mismatch for already-applied migration '001_init.sql'"):
            apply_migrations(mock_conn, sql_dir=tmp_path)

    def test_failed_migration_raises_and_rolls_back(self, tmp_path: Path):
        """SQL error during migration execution raises SchemaMigrationError."""
        (tmp_path / "001_bad.sql").write_text("INVALID SQL STATEMENT;", encoding="utf-8")

        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.transaction.return_value.__enter__.return_value = None

        mock_cur.fetchall.side_effect = [
            [],  # get_existing_tables during adoption
            [],  # get_applied_versions
            [],  # get_applied_migrations
        ]
        # Simulate execution failure on executing bad SQL
        def side_effect(sql, *args, **kwargs):
            if "INVALID SQL" in str(sql):
                raise psycopg.ProgrammingError("Syntax error in SQL statement")
            return None

        mock_cur.execute.side_effect = side_effect

        with pytest.raises(SchemaMigrationError, match="Failed executing migration '001_bad.sql'"):
            apply_migrations(mock_conn, sql_dir=tmp_path)


class TestExistingSchemaAdoptionUnit:
    """Unit tests for adopting pre-existing databases without tracking tables."""

    def test_adopt_existing_tables_success(self):
        """Pre-existing complete investigations and event_outbox tables are safely adopted."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        inv_cols = [(c,) for c in EXPECTED_TABLES["investigations"]["columns"]]
        inv_idxs = [(i,) for i in EXPECTED_TABLES["investigations"]["indexes"]]
        outbox_cols = [(c,) for c in EXPECTED_TABLES["event_outbox"]["columns"]]
        outbox_idxs = [(i,) for i in EXPECTED_TABLES["event_outbox"]["indexes"]]

        # Simulate get_existing_tables, get_applied_versions, then cols and idxs for both tables
        mock_cur.fetchall.side_effect = [
            [("investigations",), ("event_outbox",)],  # get_existing_tables
            [],  # get_applied_versions
            inv_cols,  # investigations cols
            inv_idxs,  # investigations idxs
            outbox_cols,  # event_outbox cols
            outbox_idxs,  # event_outbox idxs
        ]

        adopted = adopt_existing_schema_if_needed(mock_conn)

        assert "001_initial_schema" in adopted
        assert "002_outbox_schema" in adopted

        insert_calls = [
            c for c in mock_cur.execute.call_args_list
            if "INSERT INTO schema_migrations" in str(c)
        ]
        assert len(insert_calls) == 2

    def test_adopt_incomplete_investigations_table_rejected(self):
        """Incomplete pre-existing investigations table is rejected with SchemaMigrationError."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Missing state_payload column
        inv_cols = [
            (c,) for c in EXPECTED_TABLES["investigations"]["columns"]
            if c != "state_payload"
        ]
        inv_idxs = [(i,) for i in EXPECTED_TABLES["investigations"]["indexes"]]

        mock_cur.fetchall.side_effect = [
            [("investigations",)],  # get_existing_tables
            [],  # get_applied_versions
            inv_cols,  # investigations cols (missing state_payload)
            inv_idxs,  # investigations idxs
        ]

        with pytest.raises(SchemaMigrationError, match="Cannot adopt pre-existing table 'investigations'.*missing columns: \\['state_payload'\\]"):
            adopt_existing_schema_if_needed(mock_conn)

    def test_adopt_incomplete_outbox_table_rejected(self):
        """Incomplete pre-existing event_outbox table is rejected with SchemaMigrationError."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Valid investigations table
        inv_cols = [(c,) for c in EXPECTED_TABLES["investigations"]["columns"]]
        inv_idxs = [(i,) for i in EXPECTED_TABLES["investigations"]["indexes"]]
        # Incomplete outbox table missing payload
        outbox_cols = [
            (c,) for c in EXPECTED_TABLES["event_outbox"]["columns"]
            if c != "payload"
        ]
        outbox_idxs = [(i,) for i in EXPECTED_TABLES["event_outbox"]["indexes"]]

        mock_cur.fetchall.side_effect = [
            [("investigations",), ("event_outbox",)],  # get_existing_tables
            [],  # get_applied_versions
            inv_cols,
            inv_idxs,
            outbox_cols,
            outbox_idxs,
        ]

        with pytest.raises(SchemaMigrationError, match="Cannot adopt pre-existing table 'event_outbox'.*missing columns: \\['payload'\\]"):
            adopt_existing_schema_if_needed(mock_conn)



class TestSchemaVerificationUnit:
    """Unit tests for verify_schema checking tables, columns, and indexes."""

    def test_verify_schema_success(self):
        """Returns valid report when all tables, columns, and indexes exist."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # 1. get_existing_tables
        tables = [("schema_migrations",), ("investigations",), ("event_outbox",)]
        # 2. get_applied_versions
        applied = [("001_initial_schema",), ("002_outbox_schema",)]

        # 3. investigations columns
        inv_cols = [(col,) for col in EXPECTED_TABLES["investigations"]["columns"]]
        # 4. investigations indexes
        inv_idxs = [(idx,) for idx in EXPECTED_TABLES["investigations"]["indexes"]]

        # 5. event_outbox columns
        outbox_cols = [(col,) for col in EXPECTED_TABLES["event_outbox"]["columns"]]
        # 6. event_outbox indexes
        outbox_idxs = [(idx,) for idx in EXPECTED_TABLES["event_outbox"]["indexes"]]

        # 7. schema_migrations columns
        mig_cols = [(col,) for col in EXPECTED_TABLES["schema_migrations"]["columns"]]

        mock_cur.fetchall.side_effect = [
            tables,
            applied,
            mig_cols,
            inv_cols,
            inv_idxs,
            outbox_cols,
            outbox_idxs,
        ]

        report = verify_schema(mock_conn, raise_on_error=True)
        assert report.is_valid is True
        assert report.missing_tables == []
        assert report.missing_columns == {}
        assert report.missing_indexes == {}
        assert "001_initial_schema" in report.applied_versions

    def test_verify_schema_missing_table_raises(self):
        """Raises SchemaVerificationError when a required table is missing."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Missing event_outbox
        tables = [("schema_migrations",), ("investigations",)]
        applied = [("001_initial_schema",)]
        mig_cols = [(col,) for col in EXPECTED_TABLES["schema_migrations"]["columns"]]
        inv_cols = [(col,) for col in EXPECTED_TABLES["investigations"]["columns"]]
        inv_idxs = [(idx,) for idx in EXPECTED_TABLES["investigations"]["indexes"]]

        mock_cur.fetchall.side_effect = [tables, applied, mig_cols, inv_cols, inv_idxs]

        report = verify_schema(mock_conn, raise_on_error=False)
        assert report.is_valid is False
        assert "event_outbox" in report.missing_tables

        # Reset mock for raise_on_error test
        mock_cur.fetchall.side_effect = [tables, applied, mig_cols, inv_cols, inv_idxs]
        with pytest.raises(SchemaVerificationError, match="Missing tables: event_outbox"):
            verify_schema(mock_conn, raise_on_error=True)

    def test_verify_schema_missing_column_raises(self):
        """Raises SchemaVerificationError when a required column is missing."""
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        tables = [("schema_migrations",), ("investigations",), ("event_outbox",)]
        applied = [("001_initial_schema",), ("002_outbox_schema",)]
        mig_cols = [(col,) for col in EXPECTED_TABLES["schema_migrations"]["columns"]]
        # Missing state_payload
        inv_cols = [
            (col,) for col in EXPECTED_TABLES["investigations"]["columns"]
            if col != "state_payload"
        ]
        inv_idxs = [(idx,) for idx in EXPECTED_TABLES["investigations"]["indexes"]]
        outbox_cols = [(col,) for col in EXPECTED_TABLES["event_outbox"]["columns"]]
        outbox_idxs = [(idx,) for idx in EXPECTED_TABLES["event_outbox"]["indexes"]]

        mock_cur.fetchall.side_effect = [
            tables,
            applied,
            mig_cols,
            inv_cols,
            inv_idxs,
            outbox_cols,
            outbox_idxs,
        ]

        with pytest.raises(SchemaVerificationError, match="missing columns: state_payload"):
            verify_schema(mock_conn, raise_on_error=True)


class TestPostgresRepositoryDelegationUnit:
    """Test PostgresRepository delegating schema operations to authoritative engine."""

    def test_initialize_schema_delegation(self):
        """PostgresRepository.initialize_schema delegates to apply_migrations."""
        with patch("core.persistence.schema.apply_migrations") as mock_apply:
            mock_apply.return_value = ["001_initial_schema", "002_outbox_schema"]
            repo = PostgresRepository("postgresql://mock:mock@localhost:5432/mock_db")
            result = repo.initialize_schema()
            assert result == ["001_initial_schema", "002_outbox_schema"]
            mock_apply.assert_called_once_with(repo._pool)
            repo.close()

    def test_verify_schema_delegation(self):
        """PostgresRepository.verify_schema delegates to verify_schema."""
        mock_report = SchemaVerificationReport(
            is_valid=True,
            applied_versions=["001_initial_schema"],
            present_tables=["investigations"],
            missing_tables=[],
            missing_columns={},
            missing_indexes={},
        )
        with patch("core.persistence.schema.verify_schema") as mock_verify:
            mock_verify.return_value = mock_report
            repo = PostgresRepository("postgresql://mock:mock@localhost:5432/mock_db")
            report_dict = repo.verify_schema(raise_on_error=True)
            assert report_dict["is_valid"] is True
            mock_verify.assert_called_once_with(repo._pool, raise_on_error=True)
            repo.close()


# ============================================================================
# 2. INTEGRATION TESTS (Require Live PostgreSQL)
# ============================================================================


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL)
class TestSchemaMigrationLive:
    """Integration tests running against real PostgreSQL when active."""

    def test_live_schema_bootstrap_and_verification(self):
        """Live test applying migrations and verifying schema against PostgreSQL."""
        with PostgresRepository(PG_URL) as repo:
            applied = repo.initialize_schema()
            assert isinstance(applied, list)

            report = repo.verify_schema(raise_on_error=True)
            assert report["is_valid"] is True
            assert "investigations" in report["present_tables"]
            assert "event_outbox" in report["present_tables"]
            assert "schema_migrations" in report["present_tables"]

    def test_live_repeat_execution_idempotent(self):
        """Live test confirming repeated initialize_schema calls are safe and no-op."""
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            # Second call should apply zero new migrations
            reapplied = repo.initialize_schema()
            assert reapplied == []
