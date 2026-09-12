"""Data migration tool from FilesystemRepository to PostgresRepository.

Migrates persisted investigation states from filesystem JSON storage into PostgreSQL.
Uses only the official PersistenceRepository interfaces (no direct SQL or raw file manipulation).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Tuple

from core.persistence.filesystem import FilesystemRepository
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import PersistenceError


def migrate(
    fs_repo: FilesystemRepository,
    pg_repo: PostgresRepository,
    dry_run: bool = False,
) -> Tuple[int, int, List[str]]:
    """Migrate all investigations from FilesystemRepository to PostgresRepository.

    Args:
        fs_repo: Source FilesystemRepository.
        pg_repo: Target PostgresRepository.
        dry_run: If True, inspect and validate without writing to PostgreSQL.

    Returns:
        Tuple of (succeeded_count, failed_count, error_messages).
    """
    investigation_ids = fs_repo.list()
    succeeded = 0
    failed = 0
    errors: List[str] = []

    print(f"Found {len(investigation_ids)} investigation(s) in filesystem repository.")

    for inv_id in investigation_ids:
        try:
            state = fs_repo.load(inv_id)
            if state is None:
                err_msg = f"Failed to load investigation '{inv_id}' from filesystem."
                errors.append(err_msg)
                failed += 1
                print(f"  [ERROR] {err_msg}")
                continue

            if dry_run:
                print(f"  [DRY RUN] Would migrate '{inv_id}' (version={state.version}, status={state.status})")
                succeeded += 1
                continue

            # Save state to PostgreSQL
            pg_repo.save(state)

            # Verification round-trip
            loaded_pg = pg_repo.load(inv_id)
            if loaded_pg is None:
                err_msg = f"Verification failed for '{inv_id}': record missing in PostgreSQL after save."
                errors.append(err_msg)
                failed += 1
                print(f"  [ERROR] {err_msg}")
                continue

            orig_dict = state.to_dict()
            pg_dict = loaded_pg.to_dict()
            if orig_dict != pg_dict:
                err_msg = f"Verification failed for '{inv_id}': dict comparison mismatch."
                errors.append(err_msg)
                failed += 1
                print(f"  [ERROR] {err_msg}")
                continue

            succeeded += 1
            print(f"  [OK] Successfully migrated and verified '{inv_id}'")

        except PersistenceError as pe:
            err_msg = f"Persistence error migrating '{inv_id}': {pe}"
            errors.append(err_msg)
            failed += 1
            print(f"  [ERROR] {err_msg}")
        except Exception as e:
            err_msg = f"Unexpected error migrating '{inv_id}': {e}"
            errors.append(err_msg)
            failed += 1
            print(f"  [ERROR] {err_msg}")

    return succeeded, failed, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Sentinel investigation states from filesystem to PostgreSQL."
    )
    parser.add_argument(
        "--persistence-root",
        type=str,
        default=os.getenv("SENTINEL_PERSISTENCE_ROOT", ".sentinel_persistence"),
        help="Root directory of filesystem persistence repository.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.getenv("SENTINEL_DATABASE_URL"),
        help="PostgreSQL connection string.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without writing to database.",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="Initialize database schema before migration.",
    )

    args = parser.parse_args()

    if not args.database_url:
        print("Error: PostgreSQL database URL is required (--database-url or SENTINEL_DATABASE_URL).", file=sys.stderr)
        return 1

    fs_root = Path(args.persistence_root)
    if not fs_root.exists() or not fs_root.is_dir():
        print(f"Filesystem persistence root '{fs_root}' does not exist or is not a directory.")
        return 0

    try:
        fs_repo = FilesystemRepository(persistence_root=fs_root)
        pg_repo = PostgresRepository(pool_or_conninfo=args.database_url)

        if args.init_schema:
            print("Initializing PostgreSQL schema...")
            pg_repo.initialize_schema()

        succeeded, failed, errors = migrate(fs_repo=fs_repo, pg_repo=pg_repo, dry_run=args.dry_run)

        print("\nMigration Summary:")
        print(f"  Succeeded : {succeeded}")
        print(f"  Failed    : {failed}")

        if failed > 0:
            print("\nFailures:")
            for err in errors:
                print(f"  - {err}")
            return 1

        return 0
    except Exception as e:
        print(f"Fatal migration error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
