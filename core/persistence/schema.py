"""Authoritative PostgreSQL schema migration and verification engine for Sentinel 2.0.

Provides deterministic, ordered, and transactional execution of SQL migrations,
idempotent tracking via `schema_migrations`, existing schema adoption, and
comprehensive verification of tables, columns, and operational indexes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import psycopg
from psycopg_pool import ConnectionPool

from core.persistence.repository import PersistenceError

DEFAULT_SQL_DIR = Path(__file__).parent / "sql"


class SchemaMigrationError(PersistenceError):
    """Raised when discovering or applying a schema migration fails."""
    pass


class SchemaVerificationError(PersistenceError):
    """Raised when database schema verification fails due to missing or invalid objects."""
    pass


@dataclass(frozen=True)
class SchemaMigration:
    """Represents an discovered SQL migration file."""
    version: str
    filename: str
    path: Path
    checksum: str


@dataclass(frozen=True)
class SchemaVerificationReport:
    """Detailed report produced by schema verification."""
    is_valid: bool
    applied_versions: List[str]
    present_tables: List[str]
    missing_tables: List[str]
    missing_columns: Dict[str, List[str]]
    missing_indexes: Dict[str, List[str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "applied_versions": list(self.applied_versions),
            "present_tables": list(self.present_tables),
            "missing_tables": list(self.missing_tables),
            "missing_columns": dict(self.missing_columns),
            "missing_indexes": dict(self.missing_indexes),
        }


# Expected schema definitions for authoritative verification
EXPECTED_TABLES = {
    "schema_migrations": {
        "columns": {"version", "applied_at", "checksum"},
        "indexes": set(),
    },
    "investigations": {
        "columns": {
            "investigation_id",
            "status",
            "current_stage",
            "version",
            "llm_call_count",
            "started_at",
            "completed_at",
            "error",
            "state_payload",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "idx_investigations_status",
            "idx_investigations_current_stage",
            "idx_investigations_started_at",
        },
    },
    "event_outbox": {
        "columns": {
            "event_id",
            "investigation_id",
            "event_type",
            "payload",
            "status",
            "created_at",
            "processed_at",
            "retry_count",
            "error_message",
        },
        "indexes": {
            "idx_event_outbox_status_created",
            "idx_event_outbox_investigation_id",
            "idx_event_outbox_event_type",
        },
    },
}


@contextmanager
def acquire_connection(
    target: Union[str, ConnectionPool, psycopg.Connection]
) -> Iterator[psycopg.Connection]:
    """Yield an open psycopg.Connection from a connection string, ConnectionPool, or Connection."""
    if isinstance(target, str):
        conn_str = target.strip()
        if not conn_str:
            raise SchemaMigrationError("PostgreSQL connection string cannot be empty")
        with psycopg.connect(conn_str) as conn:
            yield conn
    elif isinstance(target, psycopg.Connection):
        yield target
    elif isinstance(target, ConnectionPool) or hasattr(target, "max_size"):
        with target.connection() as conn:
            yield conn
    elif hasattr(target, "cursor") and not hasattr(target, "connection"):
        yield target
    elif hasattr(target, "connection") and callable(getattr(target, "connection")):
        with target.connection() as conn:
            yield conn
    elif hasattr(target, "cursor"):
        yield target
    else:
        raise SchemaMigrationError(
            f"Unsupported connection target type: {type(target).__name__}. "
            "Expected connection string, ConnectionPool, or psycopg.Connection."
        )



def discover_migrations(sql_dir: Optional[Union[str, Path]] = None) -> List[SchemaMigration]:
    """Discover all SQL migration files in deterministic numeric order.

    Files must be named like '001_initial_schema.sql', '002_outbox_schema.sql'.
    Returns a sorted list of SchemaMigration dataclasses.
    """
    directory = Path(sql_dir or DEFAULT_SQL_DIR)
    if not directory.is_dir():
        raise SchemaMigrationError(f"Migration directory '{directory}' does not exist or is not a directory")

    sql_files = [f for f in directory.glob("*.sql") if f.is_file()]
    # Sort deterministically by filename
    sql_files.sort(key=lambda p: p.name)

    migrations: List[SchemaMigration] = []
    for f in sql_files:
        version = f.stem  # e.g. "001_initial_schema"
        try:
            content = f.read_bytes()
        except OSError as e:
            raise SchemaMigrationError(f"Failed reading migration file '{f}': {e}") from e

        checksum = hashlib.sha256(content).hexdigest()
        migrations.append(
            SchemaMigration(
                version=version,
                filename=f.name,
                path=f,
                checksum=checksum,
            )
        )

    return migrations


def ensure_migration_table(conn: psycopg.Connection) -> None:
    """Ensure the schema_migrations tracking table exists. Executed inside transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum VARCHAR(64)
            );
            """
        )


def get_applied_migrations(conn: psycopg.Connection) -> Dict[str, Optional[str]]:
    """Return mapping of applied migration versions to their recorded checksums."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version ASC;")
        rows = cur.fetchall()
        return {r[0]: (r[1] if len(r) > 1 else None) for r in rows}



def get_applied_versions(conn: psycopg.Connection) -> Set[str]:
    """Return the set of already-applied migration versions."""
    return set(get_applied_migrations(conn).keys())


def get_existing_tables(conn: psycopg.Connection) -> Set[str]:
    """Query information_schema for user tables present in current schema."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_type = 'BASE TABLE';
            """
        )
        return {row[0] for row in cur.fetchall()}


def get_table_columns(conn: psycopg.Connection, table_name: str) -> Set[str]:
    """Query information_schema for column names in a specific table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s;
            """,
            (table_name,),
        )
        return {row[0] for row in cur.fetchall()}


def get_table_indexes(conn: psycopg.Connection, table_name: str) -> Set[str]:
    """Query pg_indexes for index names on a specific table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = %s;
            """,
            (table_name,),
        )
        return {row[0] for row in cur.fetchall()}


def adopt_existing_schema_if_needed(conn: psycopg.Connection) -> List[str]:
    """Inspect existing database objects and record pre-existing schemas in tracking table.

    Requires full satisfaction of all expected columns and operational indexes before
    recording a pre-existing table as adopted. Incomplete schemas are rejected with
    an actionable SchemaMigrationError to prevent corrupted or partial states.

    Returns list of adopted migration versions.
    """
    existing_tables = get_existing_tables(conn)
    applied_versions = get_applied_versions(conn)
    adopted: List[str] = []

    # Check 001_initial_schema: requires complete investigations table & indexes
    if "001_initial_schema" not in applied_versions and "investigations" in existing_tables:
        cols = get_table_columns(conn, "investigations")
        idxs = get_table_indexes(conn, "investigations")
        req_cols = EXPECTED_TABLES["investigations"]["columns"]
        req_idxs = EXPECTED_TABLES["investigations"]["indexes"]

        missing_cols = req_cols - cols
        missing_idxs = req_idxs - idxs

        if missing_cols or missing_idxs:
            errors: List[str] = []
            if missing_cols:
                errors.append(f"missing columns: {sorted(list(missing_cols))}")
            if missing_idxs:
                errors.append(f"missing indexes: {sorted(list(missing_idxs))}")
            raise SchemaMigrationError(
                f"Cannot adopt pre-existing table 'investigations': schema is incomplete ({'; '.join(errors)}). "
                "Manual remediation is required before migration can proceed."
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES ('001_initial_schema', 'adopted-pre-existing-schema')
                ON CONFLICT (version) DO NOTHING;
                """
            )
        adopted.append("001_initial_schema")

    # Check 002_outbox_schema: requires complete event_outbox table & indexes
    if "002_outbox_schema" not in applied_versions and "event_outbox" in existing_tables:
        cols = get_table_columns(conn, "event_outbox")
        idxs = get_table_indexes(conn, "event_outbox")
        req_cols = EXPECTED_TABLES["event_outbox"]["columns"]
        req_idxs = EXPECTED_TABLES["event_outbox"]["indexes"]

        missing_cols = req_cols - cols
        missing_idxs = req_idxs - idxs

        if missing_cols or missing_idxs:
            errors: List[str] = []
            if missing_cols:
                errors.append(f"missing columns: {sorted(list(missing_cols))}")
            if missing_idxs:
                errors.append(f"missing indexes: {sorted(list(missing_idxs))}")
            raise SchemaMigrationError(
                f"Cannot adopt pre-existing table 'event_outbox': schema is incomplete ({'; '.join(errors)}). "
                "Manual remediation is required before migration can proceed."
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES ('002_outbox_schema', 'adopted-pre-existing-schema')
                ON CONFLICT (version) DO NOTHING;
                """
            )
        adopted.append("002_outbox_schema")

    return adopted


def apply_migrations(
    target: Union[str, ConnectionPool, psycopg.Connection],
    sql_dir: Optional[Union[str, Path]] = None,
) -> List[str]:
    """Execute all pending schema migrations in strict deterministic order.

    - Creates `schema_migrations` tracking table if missing.
    - Adopts pre-existing schema if already present on the database volume (with strict validation).
    - Verifies checksums of already applied migrations to detect tampering or drift.
    - Executes unapplied migrations sequentially inside explicit transactions.
    - Records applied versions and checksums in `schema_migrations`.
    - Rolls back cleanly on any SQL failure.

    Args:
        target: Connection string, ConnectionPool, or open psycopg.Connection.
        sql_dir: Optional path to directory containing *.sql files.

    Returns:
        List of newly applied migration versions.

    Raises:
        SchemaMigrationError: If migration discovery, SQL execution, recording, or checksum verification fails.
    """
    migrations = discover_migrations(sql_dir)
    applied_now: List[str] = []

    try:
        with acquire_connection(target) as conn:
            # 1. Ensure tracking table exists
            with conn.transaction():
                ensure_migration_table(conn)

            # 2. Adopt any existing schema created prior to tracking table
            with conn.transaction():
                adopt_existing_schema_if_needed(conn)

            # 3. Retrieve currently recorded migrations with their stored checksums
            applied_records = get_applied_migrations(conn)

            # 4. Verify checksums of already applied migrations and apply pending
            for m in migrations:
                if m.version in applied_records:
                    stored_checksum = applied_records[m.version]
                    # Verify on-disk checksum matches recorded checksum to detect file tampering
                    if stored_checksum and stored_checksum != "adopted-pre-existing-schema":
                        if stored_checksum != m.checksum:
                            raise SchemaMigrationError(
                                f"Checksum mismatch for already-applied migration '{m.filename}'. "
                                f"Database has checksum '{stored_checksum}', but current file checksum is '{m.checksum}'. "
                                "Modifying already-applied migration files is unsafe and prohibited."
                            )
                    continue

                try:
                    sql_text = m.path.read_text(encoding="utf-8")
                except OSError as e:
                    raise SchemaMigrationError(f"Cannot read migration file '{m.path}': {e}") from e

                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(sql_text)
                            cur.execute(
                                """
                                INSERT INTO schema_migrations (version, checksum)
                                VALUES (%s, %s);
                                """,
                                (m.version, m.checksum),
                            )
                except psycopg.Error as e:
                    raise SchemaMigrationError(
                        f"Failed executing migration '{m.filename}': {e}"
                    ) from e

                applied_now.append(m.version)

    except SchemaMigrationError:
        raise
    except Exception as e:
        raise SchemaMigrationError(f"Unexpected error during schema migration: {e}") from e

    return applied_now



def verify_schema(
    target: Union[str, ConnectionPool, psycopg.Connection],
    raise_on_error: bool = False,
) -> SchemaVerificationReport:
    """Verify that all required tables, columns, and operational indexes are present and valid.

    Args:
        target: Connection string, ConnectionPool, or open psycopg.Connection.
        raise_on_error: If True, raises SchemaVerificationError on any discrepancy.

    Returns:
        SchemaVerificationReport summarizing verification results.

    Raises:
        SchemaVerificationError: If raise_on_error is True and schema discrepancies exist.
        SchemaMigrationError: If database connection or inspection query fails.
    """
    try:
        with acquire_connection(target) as conn:
            existing_tables = get_existing_tables(conn)
            applied_versions = sorted(list(get_applied_versions(conn))) if "schema_migrations" in existing_tables else []

            missing_tables: List[str] = []
            missing_columns: Dict[str, List[str]] = {}
            missing_indexes: Dict[str, List[str]] = {}

            for table_name, requirements in EXPECTED_TABLES.items():
                if table_name not in existing_tables:
                    missing_tables.append(table_name)
                    continue

                present_cols = get_table_columns(conn, table_name)
                missing_cols = sorted(list(requirements["columns"] - present_cols))
                if missing_cols:
                    missing_columns[table_name] = missing_cols

                if requirements["indexes"]:
                    present_idxs = get_table_indexes(conn, table_name)
                    missing_idxs = sorted(list(requirements["indexes"] - present_idxs))
                    if missing_idxs:
                        missing_indexes[table_name] = missing_idxs

            is_valid = not (missing_tables or missing_columns or missing_indexes)

            report = SchemaVerificationReport(
                is_valid=is_valid,
                applied_versions=applied_versions,
                present_tables=sorted(list(existing_tables.intersection(EXPECTED_TABLES.keys()))),
                missing_tables=missing_tables,
                missing_columns=missing_columns,
                missing_indexes=missing_indexes,
            )

            if raise_on_error and not is_valid:
                errors: List[str] = []
                if missing_tables:
                    errors.append(f"Missing tables: {', '.join(missing_tables)}")
                for tbl, cols in missing_columns.items():
                    errors.append(f"Table '{tbl}' missing columns: {', '.join(cols)}")
                for tbl, idxs in missing_indexes.items():
                    errors.append(f"Table '{tbl}' missing indexes: {', '.join(idxs)}")
                raise SchemaVerificationError("Schema verification failed: " + "; ".join(errors))

            return report

    except (SchemaVerificationError, SchemaMigrationError):
        raise
    except Exception as e:
        raise SchemaMigrationError(f"Database error during schema verification: {e}") from e


def cli_main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for database schema migration and verification."""
    parser = argparse.ArgumentParser(
        prog="python -m core.persistence.schema",
        description="Sentinel PostgreSQL Schema Bootstrapper & Verifier",
    )
    parser.add_argument(
        "action",
        choices=["apply", "verify", "status"],
        help="Action to perform: 'apply' runs pending migrations, 'verify' checks schema completeness, 'status' shows migration tracking.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("SENTINEL_DATABASE_URL"),
        help="PostgreSQL connection URL (defaults to SENTINEL_DATABASE_URL env var)",
    )
    parser.add_argument(
        "--sql-dir",
        default=None,
        help="Directory containing migration *.sql files (defaults to core/persistence/sql)",
    )

    args = parser.parse_args(argv)

    if not args.url or not args.url.strip():
        print("ERROR: PostgreSQL connection URL must be specified via --url or SENTINEL_DATABASE_URL", file=sys.stderr)
        return 1

    try:
        if args.action == "apply":
            print(f"Applying pending migrations from '{args.sql_dir or DEFAULT_SQL_DIR}'...")
            applied = apply_migrations(args.url, sql_dir=args.sql_dir)
            if applied:
                print(f"Successfully applied {len(applied)} migration(s): {', '.join(applied)}")
            else:
                print("Database is already up to date. No pending migrations.")

            report = verify_schema(args.url, raise_on_error=True)
            print(f"Schema verification passed: {len(report.present_tables)} tables verified.")
            return 0

        elif args.action == "verify":
            print(f"Verifying schema on '{args.url}'...")
            report = verify_schema(args.url, raise_on_error=False)
            if report.is_valid:
                print(f"SUCCESS: Schema is valid. Applied versions: {', '.join(report.applied_versions)}")
                return 0
            else:
                print("FAILURE: Schema verification detected discrepancies:")
                if report.missing_tables:
                    print(f"  Missing tables: {report.missing_tables}")
                if report.missing_columns:
                    print(f"  Missing columns: {report.missing_columns}")
                if report.missing_indexes:
                    print(f"  Missing indexes: {report.missing_indexes}")
                return 1

        elif args.action == "status":
            with acquire_connection(args.url) as conn:
                existing = get_existing_tables(conn)
                if "schema_migrations" not in existing:
                    print("Tracking table 'schema_migrations' does not exist yet.")
                    return 0
                applied = get_applied_versions(conn)
                print(f"Applied migrations ({len(applied)}):")
                for v in sorted(applied):
                    print(f"  - {v}")
            return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
