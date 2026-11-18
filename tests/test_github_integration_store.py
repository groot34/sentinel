"""Tests for GitHub integration connection persistence (Mission 27.3.1).

Divided into:
1. Repository contract (shared by all backends).
2. Filesystem-specific behaviour (atomicity, reload, malformed data, traversal).
3. PostgreSQL unit tests (offline / mocked).
4. Factory behaviour.
5. Live PostgreSQL integration tests (skipped when PostgreSQL is unavailable).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from core.integrations.factory import get_integration_store
from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.postgres_store import PostgresIntegrationStore
from core.integrations.repository_store import (
    GITHUB_CREDENTIAL_REF,
    STATUS_CONNECTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    DuplicateConnectionError,
    GitHubConnection,
    IntegrationStoreError,
    check_connection_safety,
    validate_connection_id,
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
# 0. MODEL TESTS (Offline / Always Run)
# ============================================================================


class TestGitHubConnectionModel:
    """Unit tests for the non-secret GitHubConnection record."""

    def test_defaults_and_round_trip(self):
        conn = GitHubConnection(
            connection_id="ghconn_model_01",
            label="Acme / alpha",
            owner="acme",
            repo="alpha",
            scopes=["repo:read"],
        )
        assert conn.provider == "github"
        assert conn.status == STATUS_CONNECTED
        assert conn.credential_ref == GITHUB_CREDENTIAL_REF
        assert conn.visibility == "private"
        assert conn.default_branch == "main"

        restored = GitHubConnection.from_dict(conn.to_dict())
        assert restored == conn

    def test_credential_ref_must_be_env_reference(self):
        """A raw token supplied as credential_ref is rejected."""
        raw_token = "ghp_" + "a" * 40
        with pytest.raises(IntegrationStoreError, match="credential_ref must be the non-secret reference"):
            GitHubConnection(
                connection_id="ghconn_model_02",
                label="x",
                owner="acme",
                repo="alpha",
                credential_ref=raw_token,
            )

    def test_invalid_provider_rejected(self):
        with pytest.raises(IntegrationStoreError, match="Unsupported provider"):
            GitHubConnection(connection_id="ghconn_model_03", label="x", owner="a", repo="b", provider="gitlab")

    def test_invalid_status_rejected(self):
        with pytest.raises(IntegrationStoreError, match="Invalid status"):
            GitHubConnection(connection_id="ghconn_model_04", label="x", owner="a", repo="b", status="bogus")

    def test_invalid_visibility_rejected(self):
        with pytest.raises(IntegrationStoreError, match="Invalid visibility"):
            GitHubConnection(connection_id="ghconn_model_05", label="x", owner="a", repo="b", visibility="secret")

    def test_from_dict_missing_required_field(self):
        with pytest.raises(IntegrationStoreError, match="missing required field"):
            GitHubConnection.from_dict({"connection_id": "ghconn_model_06", "label": "x"})

    def test_from_dict_non_mapping(self):
        with pytest.raises(IntegrationStoreError, match="root must be an object"):
            GitHubConnection.from_dict(["not", "a", "dict"])  # type: ignore

    def test_validate_connection_id_rejects_traversal(self):
        for bad in ["../x", "a/b", "a\\b", "a:b", "", "  "]:
            with pytest.raises(IntegrationStoreError):
                validate_connection_id(bad)

    def test_validate_connection_id_accepts_and_strips(self):
        assert validate_connection_id("  ghconn_ok-1  ") == "ghconn_ok-1"

    def test_check_connection_safety_rejects_token(self):
        with pytest.raises(IntegrationStoreError, match="credential-looking value"):
            check_connection_safety(json.dumps({"x": "ghp_" + "a" * 40}))
        # The sanctioned non-secret reference is not token-shaped.
        check_connection_safety(json.dumps({"credential_ref": GITHUB_CREDENTIAL_REF}))


# ============================================================================
# 1. REPOSITORY CONTRACT (shared by all backends)
# ============================================================================


class IntegrationStoreContractTests:
    """Behavioural contract every IntegrationStore backend must satisfy."""

    def test_create_and_get(self, store):
        store.create(
            GitHubConnection(
                connection_id="ghconn_c_and_get",
                label="acme/alpha",
                owner="acme",
                repo="alpha",
                scopes=["repo:read"],
            )
        )
        loaded = store.get("ghconn_c_and_get")
        assert loaded is not None
        assert loaded.connection_id == "ghconn_c_and_get"
        assert loaded.owner == "acme"
        assert loaded.repo == "alpha"
        assert loaded.credential_ref == GITHUB_CREDENTIAL_REF
        assert loaded.status == STATUS_CONNECTED
        assert loaded.scopes == ["repo:read"]

    def test_list_connections_sorted(self, store):
        for cid, repo in [
            ("ghconn_list_a", "ra"),
            ("ghconn_list_b", "rb"),
            ("ghconn_list_c", "rc"),
        ]:
            store.create(GitHubConnection(connection_id=cid, label=cid, owner="listowner", repo=repo))

        ids = [c.connection_id for c in store.list() if c.connection_id.startswith("ghconn_list_")]
        assert ids == ["ghconn_list_a", "ghconn_list_b", "ghconn_list_c"]

    def test_update_metadata_and_status(self, store):
        store.create(GitHubConnection(connection_id="ghconn_update", label="Old", owner="upowner", repo="uprepo"))

        updated = store.update(
            "ghconn_update",
            label="New",
            status=STATUS_ERROR,
            default_branch="develop",
            visibility="public",
            scopes=["repo:status"],
            last_verified_at="2026-01-01T00:00:00+00:00",
        )
        assert updated.label == "New"
        assert updated.status == STATUS_ERROR
        assert updated.default_branch == "develop"
        assert updated.visibility == "public"
        assert updated.scopes == ["repo:status"]
        assert updated.last_verified_at is not None and updated.last_verified_at.startswith("2026-01-01")

        reloaded = store.get("ghconn_update")
        assert reloaded is not None
        assert reloaded.status == STATUS_ERROR
        # Immutable identity fields are preserved.
        assert reloaded.owner == "upowner"
        assert reloaded.repo == "uprepo"

    def test_update_missing_raises(self, store):
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_does_not_exist", label="x")

    def test_delete_idempotent(self, store):
        store.create(GitHubConnection(connection_id="ghconn_delete", label="d", owner="delowner", repo="delrepo"))
        assert store.exists("ghconn_delete")

        store.delete("ghconn_delete")
        assert not store.exists("ghconn_delete")
        assert store.get("ghconn_delete") is None

        # Idempotent: repeated and never-existing deletes are safe.
        store.delete("ghconn_delete")
        store.delete("ghconn_never_existed")

    def test_exists_behaviour(self, store):
        assert not store.exists("ghconn_exists_no")
        store.create(GitHubConnection(connection_id="ghconn_exists_yes", label="e", owner="exowner", repo="exrepo"))
        assert store.exists("ghconn_exists_yes")

    def test_missing_returns_none(self, store):
        assert store.get("ghconn_missing_id") is None

    def test_duplicate_connection_id(self, store):
        store.create(GitHubConnection(connection_id="ghconn_dup_id", label="a", owner="dupidowner", repo="dupidrepo"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_dup_id", label="b", owner="other", repo="other"))

    def test_duplicate_owner_repo(self, store):
        store.create(GitHubConnection(connection_id="ghconn_dup_repo_a", label="a", owner="dupowner", repo="duprepo"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_dup_repo_b", label="b", owner="dupowner", repo="duprepo"))

    def test_duplicate_owner_repo_case_insensitive(self, store):
        """GitHub owner/repo are case-insensitive: a case variant is a duplicate."""
        store.create(GitHubConnection(connection_id="ghconn_case_a", label="a", owner="groot34", repo="Sentinel"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_case_b", label="b", owner="groot34", repo="sentinel"))

    def test_duplicate_owner_repo_case_insensitive_reverse(self, store):
        """The reverse casing order is also rejected."""
        store.create(GitHubConnection(connection_id="ghconn_case_c", label="a", owner="Groot34", repo="sentinel"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_case_d", label="b", owner="groot34", repo="Sentinel"))

    def test_update_invalid_timestamp_rejected(self, store):
        """Invalid timestamps are rejected consistently on both backends."""
        store.create(GitHubConnection(connection_id="ghconn_bad_ts", label="x", owner="tsowner", repo="tsrepo"))
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_bad_ts", last_verified_at="not-a-timestamp")

    def test_update_invalid_visibility_rejected(self, store):
        """Invalid visibility is rejected consistently on both backends."""
        store.create(GitHubConnection(connection_id="ghconn_bad_vis", label="x", owner="visowner", repo="visrepo"))
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_bad_vis", visibility="secret")

    def test_update_invalid_status_rejected(self, store):
        """Invalid status is rejected consistently on both backends."""
        store.create(GitHubConnection(connection_id="ghconn_bad_status", label="x", owner="stowner", repo="strepo"))
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_bad_status", status="bogus")

    def test_rejected_update_leaves_record_unchanged(self, store):
        """A failed update must not alter the previously stored record."""
        store.create(
            GitHubConnection(
                connection_id="ghconn_stable",
                label="Original",
                owner="stableowner",
                repo="stablerepo",
                scopes=["repo:read"],
            )
        )
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_stable", status="bogus")
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_stable", visibility="secret")

        reloaded = store.get("ghconn_stable")
        assert reloaded is not None
        assert reloaded.status == STATUS_CONNECTED
        assert reloaded.visibility == "private"
        assert reloaded.label == "Original"
        assert reloaded.scopes == ["repo:read"]

    @pytest.mark.parametrize(
        "bad_id",
        ["../traversal", r"..\traversal", "/absolute/path", "nested/id", ":colon", "has space", ""],
    )
    def test_invalid_connection_id_rejected(self, store, bad_id: str):
        with pytest.raises(IntegrationStoreError):
            store.get(bad_id)
        with pytest.raises(IntegrationStoreError):
            store.exists(bad_id)

    def test_create_requires_connection_type(self, store):
        with pytest.raises(IntegrationStoreError, match="accepts only GitHubConnection"):
            store.create({"connection_id": "ghconn_not_a_model"})  # type: ignore


# ============================================================================
# 2. FILESYSTEM TESTS
# ============================================================================


class TestFilesystemIntegrationStoreContract(IntegrationStoreContractTests):
    """Run the shared contract against FilesystemIntegrationStore."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> FilesystemIntegrationStore:
        return FilesystemIntegrationStore(tmp_path)


class TestFilesystemIntegrationStoreSpecific:
    """Filesystem-specific durability and safety behaviour."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> FilesystemIntegrationStore:
        return FilesystemIntegrationStore(tmp_path)

    def _connection(self, connection_id: str = "ghconn_fs_01", **kwargs) -> GitHubConnection:
        return GitHubConnection(
            connection_id=connection_id,
            label=kwargs.pop("label", "acme/alpha"),
            owner=kwargs.pop("owner", "acme"),
            repo=kwargs.pop("repo", "alpha"),
            **kwargs,
        )

    def test_atomic_write_leaves_no_temp_files(self, store: FilesystemIntegrationStore, tmp_path: Path):
        store.create(self._connection())
        integrations_dir = tmp_path / "integrations"
        assert (integrations_dir / "ghconn_fs_01.json").is_file()
        assert list(integrations_dir.glob("*.tmp")) == []

    def test_reload_after_reinitialisation(self, tmp_path: Path):
        """A new store instance over the same root sees persisted records."""
        FilesystemIntegrationStore(tmp_path).create(
            self._connection("ghconn_fs_reload", owner="reloadowner", repo="reloadrepo")
        )
        reopened = FilesystemIntegrationStore(tmp_path)
        loaded = reopened.get("ghconn_fs_reload")
        assert loaded is not None
        assert loaded.repo == "reloadrepo"

    def test_invalid_json_raises(self, store: FilesystemIntegrationStore, tmp_path: Path):
        integrations_dir = tmp_path / "integrations"
        integrations_dir.mkdir(parents=True, exist_ok=True)
        (integrations_dir / "ghconn_badjson.json").write_text("{not valid json", encoding="utf-8")

        with pytest.raises(IntegrationStoreError, match="Malformed JSON"):
            store.get("ghconn_badjson")

    def test_malformed_record_raises(self, store: FilesystemIntegrationStore, tmp_path: Path):
        integrations_dir = tmp_path / "integrations"
        integrations_dir.mkdir(parents=True, exist_ok=True)
        # Missing required owner/repo fields.
        (integrations_dir / "ghconn_badrec.json").write_text(
            json.dumps({"connection_id": "ghconn_badrec", "label": "x"}), encoding="utf-8"
        )

        with pytest.raises(IntegrationStoreError):
            store.get("ghconn_badrec")

    def test_connection_id_mismatch_detected(self, store: FilesystemIntegrationStore, tmp_path: Path):
        integrations_dir = tmp_path / "integrations"
        integrations_dir.mkdir(parents=True, exist_ok=True)
        data = self._connection("ghconn_other").to_dict()
        (integrations_dir / "ghconn_mismatch.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(IntegrationStoreError, match="mismatch"):
            store.get("ghconn_mismatch")

    def test_path_traversal_rejected(self, store: FilesystemIntegrationStore):
        with pytest.raises(IntegrationStoreError):
            store.get("../../etc/passwd")

    def test_update_preserves_other_records(self, store: FilesystemIntegrationStore, tmp_path: Path):
        store.create(self._connection("ghconn_keep_a", owner="keepowner", repo="keeprepoa"))
        store.create(self._connection("ghconn_keep_b", owner="keepowner", repo="keeprepob"))

        other_file = tmp_path / "integrations" / "ghconn_keep_b.json"
        before = other_file.read_bytes()

        store.update("ghconn_keep_a", status=STATUS_DISABLED)

        assert other_file.read_bytes() == before
        assert store.get("ghconn_keep_a").status == STATUS_DISABLED  # type: ignore[union-attr]

    def test_does_not_overwrite_unrelated_files(self, store: FilesystemIntegrationStore, tmp_path: Path):
        integrations_dir = tmp_path / "integrations"
        integrations_dir.mkdir(parents=True, exist_ok=True)
        unrelated = integrations_dir / "notes.txt"
        unrelated.write_text("keep me", encoding="utf-8")

        store.create(self._connection("ghconn_unrelated", owner="unrelowner", repo="unrelrepo"))

        assert unrelated.read_text(encoding="utf-8") == "keep me"

    def test_no_token_value_persisted(self, store: FilesystemIntegrationStore, tmp_path: Path):
        store.create(self._connection("ghconn_no_token", owner="tokowner", repo="tokrepo"))
        raw = (tmp_path / "integrations" / "ghconn_no_token.json").read_text(encoding="utf-8")

        assert GITHUB_CREDENTIAL_REF in raw
        assert "ghp_" not in raw
        assert "github_pat_" not in raw

    def test_original_casing_preserved(self, store: FilesystemIntegrationStore):
        store.create(self._connection("ghconn_casing", owner="Groot34", repo="SenTineL"))
        loaded = store.get("ghconn_casing")
        assert loaded is not None
        assert loaded.owner == "Groot34"
        assert loaded.repo == "SenTineL"

    def test_concurrent_case_variant_create_single_winner(self, tmp_path: Path):
        """Concurrent creates for case-variant (owner, repo) cannot both succeed."""
        store = FilesystemIntegrationStore(tmp_path)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(owner: str, repo: str, connection_id: str) -> None:
            barrier.wait()
            try:
                store.create(
                    GitHubConnection(connection_id=connection_id, label=connection_id, owner=owner, repo=repo)
                )
                result = "ok"
            except DuplicateConnectionError:
                result = "dup"
            with lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=worker, args=("groot34", "Sentinel", "ghconn_race_a")),
            threading.Thread(target=worker, args=("groot34", "sentinel", "ghconn_race_b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == ["dup", "ok"]
        # Exactly one record exists, so the pair can never both be persisted.
        assert len(store.list()) == 1

    def test_concurrent_same_id_create_single_winner(self, tmp_path: Path):
        """Concurrent creates for the same connection ID cannot both succeed."""
        store = FilesystemIntegrationStore(tmp_path)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                store.create(
                    GitHubConnection(connection_id="ghconn_race_id", label="x", owner="raceowner", repo="racerepo")
                )
                result = "ok"
            except DuplicateConnectionError:
                result = "dup"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == ["dup", "ok"]
        assert store.exists("ghconn_race_id")

    def test_same_root_instances_share_create_lock(self, tmp_path: Path):
        """Separate instances on the same resolved root share one lock object."""
        a = FilesystemIntegrationStore(tmp_path)
        b = FilesystemIntegrationStore(tmp_path)
        assert a._create_lock is b._create_lock

    def test_equivalent_root_spellings_share_create_lock(self, tmp_path: Path):
        """Equivalent spellings of the same directory collapse to one lock."""
        a = FilesystemIntegrationStore(tmp_path)
        b = FilesystemIntegrationStore(tmp_path / ".")
        assert a._create_lock is b._create_lock

    def test_different_root_instances_have_independent_locks(self, tmp_path: Path):
        """Instances on different directories must not share a lock."""
        a = FilesystemIntegrationStore(tmp_path / "root_a")
        b = FilesystemIntegrationStore(tmp_path / "root_b")
        assert a._create_lock is not b._create_lock

    def test_lock_registry_concurrent_initialisation(self, tmp_path: Path):
        """Concurrent registry initialisation yields exactly one lock per root."""
        from core.integrations.filesystem_store import _lock_for_root

        root = tmp_path / "concurrent_root"
        threads: list[threading.Thread] = []
        results: list = []
        collector = threading.Lock()
        barrier = threading.Barrier(16)

        def worker() -> None:
            barrier.wait()
            lock = _lock_for_root(root)
            with collector:
                results.append(lock)

        for _ in range(16):
            threads.append(threading.Thread(target=worker))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 16
        assert len({id(lock) for lock in results}) == 1

    def test_concurrent_case_variant_create_across_instances_single_winner(self, tmp_path: Path):
        """Two separate instances on one root cannot both create a case variant."""
        store_a = FilesystemIntegrationStore(tmp_path)
        store_b = FilesystemIntegrationStore(tmp_path)
        assert store_a._create_lock is store_b._create_lock

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(store: FilesystemIntegrationStore, owner: str, repo: str, connection_id: str) -> None:
            barrier.wait()
            try:
                store.create(
                    GitHubConnection(connection_id=connection_id, label=connection_id, owner=owner, repo=repo)
                )
                result = "ok"
            except DuplicateConnectionError:
                result = "dup"
            with lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=worker, args=(store_a, "groot34", "Sentinel", "ghconn_xinst_a")),
            threading.Thread(target=worker, args=(store_b, "groot34", "sentinel", "ghconn_xinst_b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == ["dup", "ok"]
        assert len(store_a.list()) == 1
        assert len(store_b.list()) == 1


# ============================================================================
# 3. POSTGRESQL UNIT TESTS (Offline / Mocked)
# ============================================================================


class TestPostgresIntegrationStoreUnit:
    """Unit tests for PostgresIntegrationStore logic without a live database."""

    def test_constructor_validation(self):
        with pytest.raises(IntegrationStoreError, match="cannot be empty"):
            PostgresIntegrationStore("")
        with pytest.raises(IntegrationStoreError, match="Expected ConnectionPool or conninfo"):
            PostgresIntegrationStore(12345)  # type: ignore

    def test_from_env_validation(self, monkeypatch):
        monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
        with pytest.raises(IntegrationStoreError, match="is not set or empty"):
            PostgresIntegrationStore.from_env()

    def _mock_store(self):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pool.connection.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        return PostgresIntegrationStore(mock_pool), mock_cur

    def test_create_requires_connection_type(self):
        store, _ = self._mock_store()
        with pytest.raises(IntegrationStoreError, match="accepts only GitHubConnection"):
            store.create({"connection_id": "x"})  # type: ignore

    def test_create_unique_violation_maps_to_duplicate(self):
        store, mock_cur = self._mock_store()
        mock_cur.execute.side_effect = psycopg.errors.UniqueViolation("dup")

        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_u1", label="x", owner="a", repo="b"))

    def test_create_database_error_wrapped(self):
        store, mock_cur = self._mock_store()
        mock_cur.execute.side_effect = psycopg.OperationalError("down")

        with pytest.raises(IntegrationStoreError, match="Database error creating"):
            store.create(GitHubConnection(connection_id="ghconn_u2", label="x", owner="a", repo="b"))

    def test_get_missing_returns_none(self):
        store, mock_cur = self._mock_store()
        mock_cur.fetchone.return_value = None
        assert store.get("ghconn_u3") is None

    def test_update_missing_raises(self):
        store, mock_cur = self._mock_store()
        mock_cur.fetchone.return_value = None
        with pytest.raises(IntegrationStoreError, match="not found"):
            store.update("ghconn_u4", label="x")

    def test_delete_invalid_id_is_noop(self):
        store, mock_cur = self._mock_store()
        store.delete("../bad")
        mock_cur.execute.assert_not_called()

    def test_exists_invalid_id_rejected(self):
        store, _ = self._mock_store()
        with pytest.raises(IntegrationStoreError):
            store.exists("/absolute")

    def test_transaction_uses_parameterised_sql(self):
        """create() passes values as query parameters, never string-interpolated."""
        store, mock_cur = self._mock_store()
        store.create(GitHubConnection(connection_id="ghconn_param", label="x", owner="a", repo="b"))

        assert mock_cur.execute.call_count == 1
        sql, params = mock_cur.execute.call_args[0]
        assert "INSERT INTO github_integrations" in sql
        assert isinstance(params, tuple)
        assert "ghconn_param" in params


# ============================================================================
# 4. FACTORY TESTS (Offline / Always Run)
# ============================================================================


class TestIntegrationStoreFactory:
    """Unit tests for get_integration_store()."""

    def test_default_is_filesystem(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SENTINEL_PERSISTENCE_BACKEND", raising=False)
        monkeypatch.delenv("SENTINEL_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("SENTINEL_PERSISTENCE_ROOT", str(tmp_path))

        store = get_integration_store()
        assert isinstance(store, FilesystemIntegrationStore)
        assert store.persistence_root == tmp_path.resolve()

    def test_explicit_filesystem_backend(self, tmp_path):
        store = get_integration_store(backend="filesystem", persistence_root=tmp_path)
        assert isinstance(store, FilesystemIntegrationStore)

    def test_postgres_missing_url_raises_error(self, monkeypatch):
        monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
        with pytest.raises(IntegrationStoreError, match="SENTINEL_DATABASE_URL is not configured"):
            get_integration_store(backend="postgres")

    def test_postgres_explicit_url(self):
        store = get_integration_store(
            backend="postgres",
            database_url="postgresql://user:pass@localhost:5432/test_db",
        )
        assert isinstance(store, PostgresIntegrationStore)
        store.close()

    def test_invalid_backend_raises_error(self):
        with pytest.raises(IntegrationStoreError, match="Unsupported integration store backend"):
            get_integration_store(backend="sqlite")


# ============================================================================
# 5. MIGRATION ARTEFACT TESTS (Offline / Always Run)
# ============================================================================


class TestMigrationArtefact:
    """Static checks on migration 005."""

    def test_migration_005_has_no_token_column(self):
        import re

        from core.persistence.schema import DEFAULT_SQL_DIR

        text = (DEFAULT_SQL_DIR / "005_github_integrations.sql").read_text(encoding="utf-8")
        assert "github_integrations" in text
        assert "credential_ref" in text

        match = re.search(
            r"CREATE TABLE IF NOT EXISTS github_integrations \((.*?)\);", text, re.S
        )
        assert match is not None, "github_integrations CREATE TABLE not found"
        column_names = [
            line.strip().split()[0].lower()
            for line in match.group(1).strip().splitlines()
            if line.strip() and not line.strip().upper().startswith("CONSTRAINT")
        ]
        assert "credential_ref" in column_names
        assert not any("token" in c for c in column_names)
        assert not any("secret" in c for c in column_names)

    def test_expected_tables_includes_github_integrations(self):
        from core.persistence.schema import EXPECTED_TABLES

        assert "github_integrations" in EXPECTED_TABLES
        columns = EXPECTED_TABLES["github_integrations"]["columns"]
        assert "credential_ref" in columns
        assert "status" in columns
        assert "token" not in columns

    def test_migration_006_replaces_index_case_insensitively(self):
        """Static checks on migration 006: drop + case-insensitive unique index."""
        import re

        from core.persistence.schema import DEFAULT_SQL_DIR

        path = DEFAULT_SQL_DIR / "006_github_integrations_case_insensitive_unique.sql"
        assert path.is_file(), "migration 006 not found"

        text = path.read_text(encoding="utf-8")
        assert "DROP INDEX IF EXISTS idx_github_integrations_owner_repo" in text

        lowered = text.lower()
        assert "lower(owner)" in lowered
        assert "lower(repo)" in lowered
        assert "unique index" in lowered
        # Conflict detection must abort rather than delete data.
        assert "raise exception" in lowered
        assert "delete from" not in lowered

        assert re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_github_integrations_owner_repo", text)

    def test_migration_006_discovered_by_engine(self):
        from core.persistence.schema import DEFAULT_SQL_DIR, discover_migrations

        versions = [m.version for m in discover_migrations(DEFAULT_SQL_DIR)]
        assert "006_github_integrations_case_insensitive_unique" in versions
        assert versions.index("005_github_integrations") < versions.index(
            "006_github_integrations_case_insensitive_unique"
        )


# ============================================================================
# 6. LIVE POSTGRESQL INTEGRATION TESTS
# ============================================================================


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL)
class TestPostgresIntegrationStoreLive(IntegrationStoreContractTests):
    """Run the shared contract against a real PostgreSQL instance."""

    @pytest.fixture(scope="class", autouse=True)
    def setup_schema(self):
        with PostgresIntegrationStore(PG_URL) as store:
            store.initialize_schema()
            store.verify_schema(raise_on_error=True)

    @pytest.fixture
    def store(self):
        s = PostgresIntegrationStore(PG_URL)
        yield s
        try:
            with s.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM github_integrations WHERE connection_id LIKE 'ghconn_%';")
        except Exception:
            pass
        s.close()


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL)
class TestPostgresIntegrationStoreLiveSpecific:
    """Live PostgreSQL behaviour beyond the shared contract."""

    @pytest.fixture
    def store(self):
        s = PostgresIntegrationStore(PG_URL)
        s.initialize_schema()
        yield s
        try:
            with s.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM github_integrations WHERE connection_id LIKE 'ghconn_%';")
        except Exception:
            pass
        s.close()

    def test_migration_registers_table_and_schema_verifies(self, store: PostgresIntegrationStore):
        report = store.verify_schema(raise_on_error=True)
        assert report["is_valid"] is True
        assert "github_integrations" in report["present_tables"]

    def test_case_insensitive_unique_index_applied(self, store: PostgresIntegrationStore):
        """Migration 006 must leave a case-insensitive unique index in place."""
        with store.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT indexdef FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'github_integrations'
                      AND indexname = 'idx_github_integrations_owner_repo';
                    """
                )
                row = cur.fetchone()

        assert row is not None, "case-insensitive unique index not found"
        indexdef = row[0].lower()
        assert "unique" in indexdef
        # PostgreSQL renders expression indexes as lower((owner)::text).
        assert "lower" in indexdef
        assert "owner" in indexdef
        assert "repo" in indexdef

    def test_owner_repo_case_variant_rejected_live(self, store: PostgresIntegrationStore):
        store.create(GitHubConnection(connection_id="ghconn_live_case_a", label="a", owner="groot34", repo="Sentinel"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_live_case_b", label="b", owner="groot34", repo="sentinel"))

    def test_no_token_column_present(self, store: PostgresIntegrationStore):
        with store.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'github_integrations';
                    """
                )
                columns = {row[0] for row in cur.fetchall()}

        assert "credential_ref" in columns
        assert "token" not in columns
        assert not any("token" in c.lower() for c in columns)

    def test_status_constraint_enforced(self, store: PostgresIntegrationStore):
        store.create(GitHubConnection(connection_id="ghconn_live_status", label="x", owner="liveowner", repo="liverepo"))
        with pytest.raises(IntegrationStoreError):
            store.update("ghconn_live_status", status="not_a_status")

    def test_owner_repo_uniqueness_enforced(self, store: PostgresIntegrationStore):
        store.create(GitHubConnection(connection_id="ghconn_live_uniq_a", label="a", owner="uniqowner", repo="uniqrepo"))
        with pytest.raises(DuplicateConnectionError):
            store.create(GitHubConnection(connection_id="ghconn_live_uniq_b", label="b", owner="uniqowner", repo="uniqrepo"))

    def test_no_credential_values_stored(self, store: PostgresIntegrationStore):
        store.create(GitHubConnection(connection_id="ghconn_live_secret", label="x", owner="secowner", repo="secrepo"))

        with store.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT credential_ref, scopes::text, label, owner, repo
                    FROM github_integrations WHERE connection_id = %s;
                    """,
                    ("ghconn_live_secret",),
                )
                row = cur.fetchone()

        assert row is not None
        assert row[0] == GITHUB_CREDENTIAL_REF
        blob = " ".join(str(v) for v in row)
        assert "ghp_" not in blob
        assert "github_pat_" not in blob
