"""Reusable behavioural contract test suite for PersistenceRepository implementations.

Tests that both FilesystemRepository and PostgresRepository fulfill the identical
architectural contract and invariants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.persistence.filesystem import FilesystemRepository
from core.persistence.repository import PersistenceError, PersistenceRepository


class PersistenceContractTests:
    """Base test contract that any valid PersistenceRepository must satisfy."""

    def test_save_load_round_trip(self, repo: PersistenceRepository):
        """Saving an InvestigationState and loading it back preserves all fields."""
        state = InvestigationState(incident_id="contract_test_01")
        state.start_stage("logs")
        state.complete_stage(
            "logs",
            output={"incident_id": "contract_test_01", "evidence": []},
            llm_calls=2,
            total_tokens=350,
        )
        state.add_evidence({
            "evidence_id": "EV-LOG-001",
            "type": "error",
            "reference": "application.log:42",
            "excerpt": "Connection timed out after 30000ms",
        })

        repo.save(state)
        loaded = repo.load("contract_test_01")

        assert loaded is not None
        assert loaded.incident_id == "contract_test_01"
        assert loaded.status == IncidentStatus.RUNNING
        assert "logs" in loaded.stages
        assert loaded.stages["logs"].status == StageStatus.SUCCEEDED
        assert loaded.stages["logs"].llm_calls == 2
        assert loaded.stages["logs"].total_tokens == 350
        assert len(loaded.evidence) == 1
        assert loaded.evidence[0].evidence_id == "EV-LOG-001"
        assert loaded.version >= 1

    def test_overwrite_updated_state(self, repo: PersistenceRepository):
        """Subsequent saves correctly update existing state with new stages and data."""
        state = InvestigationState(incident_id="contract_test_02")
        state.start_stage("logs")
        state.complete_stage("logs", output={"log": "done"})
        repo.save(state)

        # Progress to next stage
        state.start_stage("metrics")
        state.complete_stage("metrics", output={"metric": "done"})
        repo.save(state)

        loaded = repo.load("contract_test_02")
        assert loaded is not None
        assert "logs" in loaded.stages
        assert "metrics" in loaded.stages
        assert loaded.stages["metrics"].status == StageStatus.SUCCEEDED

    def test_exists_behaviour(self, repo: PersistenceRepository):
        """exists() accurately reports whether durable state is present."""
        assert not repo.exists("contract_non_existent")

        state = InvestigationState(incident_id="contract_test_03")
        repo.save(state)

        assert repo.exists("contract_test_03")
        assert not repo.exists("contract_non_existent")

    def test_load_missing_returns_none(self, repo: PersistenceRepository):
        """Loading a non-existent investigation ID returns None without raising."""
        assert repo.load("contract_missing_id") is None

    def test_delete_idempotent(self, repo: PersistenceRepository):
        """delete() removes an existing record and is safely idempotent."""
        state = InvestigationState(incident_id="contract_to_delete")
        repo.save(state)
        assert repo.exists("contract_to_delete")

        repo.delete("contract_to_delete")
        assert not repo.exists("contract_to_delete")
        assert repo.load("contract_to_delete") is None

        # Idempotent second delete does not raise
        repo.delete("contract_to_delete")
        repo.delete("never_existed")

    def test_list_deterministic_ordering(self, repo: PersistenceRepository):
        """list() returns existing investigation IDs in deterministic alphabetical order."""
        for inc_id in ["contract_zebra", "contract_alpha", "contract_mango"]:
            state = InvestigationState(incident_id=inc_id)
            repo.save(state)

        ids = [i for i in repo.list() if i.startswith("contract_")]
        assert ids == ["contract_alpha", "contract_mango", "contract_zebra"]

    @pytest.mark.parametrize("bad_id", [
        "../traversal",
        r"..\traversal",
        "/absolute/path",
        r"C:\windows\path",
        "nested/id",
        ":invalid_colon",
        "has spaces in id",
        "",
    ])
    def test_invalid_investigation_id_rejected(self, repo: PersistenceRepository, bad_id: str):
        """Unsafe IDs, paths, and traversal characters are rejected by all methods."""
        with pytest.raises(PersistenceError):
            repo.load(bad_id)

        with pytest.raises(PersistenceError):
            repo.exists(bad_id)

    def test_forbidden_benchmark_artefact_rejected(self, repo: PersistenceRepository):
        """State referencing forbidden ground_truth or baseline files is rejected on save."""
        state = InvestigationState(incident_id="contract_forbidden_gt")
        gt_token = "ground" + "_truth.md"
        state.metadata["forbidden"] = gt_token

        with pytest.raises(PersistenceError, match="forbidden benchmark artefact detected"):
            repo.save(state)

    def test_forbidden_secret_rejected(self, repo: PersistenceRepository):
        """State referencing API key secret variable names is rejected on save."""
        state = InvestigationState(incident_id="contract_forbidden_sec")
        sec_token = "GROQ" + "_API_KEY"
        state.metadata["secret"] = {sec_token: "gsk_12345"}

        with pytest.raises(PersistenceError, match="forbidden secret key detected"):
            repo.save(state)

    def test_save_requires_investigation_state_type(self, repo: PersistenceRepository):
        """save() rejects raw dicts or non-InvestigationState types."""
        with pytest.raises(PersistenceError, match="accepts only InvestigationState"):
            repo.save({"incident_id": "fake", "status": "RUNNING"})  # type: ignore

    def test_version_preserved_on_round_trip(self, repo: PersistenceRepository):
        """InvestigationState version is preserved on save and load."""
        state = InvestigationState(incident_id="contract_version_test", version=1)
        repo.save(state)
        loaded = repo.load("contract_version_test")
        assert loaded is not None
        assert loaded.version >= 1


# ---------------------------------------------------------------------------
# Test the contract against FilesystemRepository
# ---------------------------------------------------------------------------

class TestFilesystemRepositoryContract(PersistenceContractTests):
    """Run full PersistenceContractTests suite against FilesystemRepository."""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> FilesystemRepository:
        return FilesystemRepository(persistence_root=tmp_path)
