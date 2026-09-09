"""Tests for Sentinel 2.0 Persistence Repository (FilesystemRepository)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.persistence.filesystem import FilesystemRepository
from core.persistence.repository import PersistenceError, PersistenceRepository


@pytest.fixture
def repo(tmp_path: Path) -> FilesystemRepository:
    """Fixture providing a FilesystemRepository backed by a temporary directory."""
    return FilesystemRepository(persistence_root=tmp_path)


def test_repository_interface(repo: FilesystemRepository):
    """FilesystemRepository must implement PersistenceRepository interface."""
    assert isinstance(repo, PersistenceRepository)


def test_save_load_round_trip(repo: FilesystemRepository):
    """Test 1: Save state and load it back, verifying field fidelity."""
    state = InvestigationState(incident_id="incident_01")
    state.start_stage("logs")
    state.complete_stage("logs", output={"incident_id": "incident_01", "evidence": []}, llm_calls=1)
    state.add_evidence({
        "evidence_id": "EV-LOG-001",
        "type": "error",
        "reference": "api.log:123",
        "excerpt": "500 Internal Server Error",
    })

    repo.save(state)
    loaded = repo.load("incident_01")

    assert loaded is not None
    assert loaded.incident_id == "incident_01"
    assert loaded.status == IncidentStatus.RUNNING
    assert "logs" in loaded.stages
    assert loaded.stages["logs"].status == StageStatus.SUCCEEDED
    assert loaded.stages["logs"].llm_calls == 1
    assert len(loaded.evidence) == 1
    assert loaded.evidence[0].evidence_id == "EV-LOG-001"


def test_save_same_state_twice(repo: FilesystemRepository):
    """Test 2: Saving the same state twice is idempotent and safe."""
    state = InvestigationState(incident_id="incident_02")
    state.start_stage("logs")
    state.complete_stage("logs", output={"data": 123})

    repo.save(state)
    repo.save(state)

    loaded = repo.load("incident_02")
    assert loaded is not None
    assert loaded.incident_id == "incident_02"
    assert "logs" in loaded.stages


def test_overwrite_updated_state(repo: FilesystemRepository):
    """Test 3: Overwrite state with updated stages and verify updates."""
    state = InvestigationState(incident_id="incident_03")
    state.start_stage("logs")
    state.complete_stage("logs", output={"stage": "logs"})
    repo.save(state)

    # Update state with next stage
    state.start_stage("metrics")
    state.complete_stage("metrics", output={"stage": "metrics"})
    repo.save(state)

    loaded = repo.load("incident_03")
    assert loaded is not None
    assert "logs" in loaded.stages
    assert "metrics" in loaded.stages
    assert loaded.stages["metrics"].status == StageStatus.SUCCEEDED


def test_load_missing_state(repo: FilesystemRepository):
    """Test 4: Loading a non-existent state returns None."""
    assert repo.load("non_existent_inc") is None
    assert not repo.exists("non_existent_inc")


def test_load_malformed_json(repo: FilesystemRepository, tmp_path: Path):
    """Test 5: Malformed JSON in state file raises clear PersistenceError."""
    inv_dir = tmp_path / "corrupt_inc"
    inv_dir.mkdir(parents=True)
    (inv_dir / "state.json").write_text("{corrupt: json-content,,,", encoding="utf-8")

    with pytest.raises(PersistenceError, match="Malformed JSON"):
        repo.load("corrupt_inc")


def test_load_invalid_investigation_state(repo: FilesystemRepository, tmp_path: Path):
    """Test 6: Structurally invalid InvestigationState JSON raises PersistenceError."""
    inv_dir = tmp_path / "invalid_inc"
    inv_dir.mkdir(parents=True)
    # Root not a dictionary
    (inv_dir / "state.json").write_text("[\"not\", \"a\", \"dict\"]", encoding="utf-8")

    with pytest.raises(PersistenceError, match="root must be a dict"):
        repo.load("invalid_inc")

    # Empty incident_id fails validation
    (inv_dir / "state.json").write_text(json.dumps({"incident_id": ""}), encoding="utf-8")
    with pytest.raises(PersistenceError, match="Failed to deserialize InvestigationState"):
        repo.load("invalid_inc")


def test_incident_id_mismatch(repo: FilesystemRepository, tmp_path: Path):
    """Test 7: Loaded incident_id does not match requested directory ID."""
    inv_dir = tmp_path / "inc_alpha"
    inv_dir.mkdir(parents=True)
    state_dict = {"incident_id": "inc_beta", "status": "RUNNING"}
    (inv_dir / "state.json").write_text(json.dumps(state_dict), encoding="utf-8")

    with pytest.raises(PersistenceError, match="Investigation ID mismatch"):
        repo.load("inc_alpha")


def test_delete(repo: FilesystemRepository):
    """Test 8: Delete removes investigation state and directory."""
    state = InvestigationState(incident_id="inc_to_delete")
    repo.save(state)
    assert repo.exists("inc_to_delete")

    repo.delete("inc_to_delete")
    assert not repo.exists("inc_to_delete")
    assert repo.load("inc_to_delete") is None
    # Empty dir was removed
    assert not (repo.persistence_root / "inc_to_delete").exists()


def test_delete_idempotent(repo: FilesystemRepository):
    """Test 9: Deleting non-existent investigation is a safe no-op."""
    repo.delete("does_not_exist")  # Should not raise


def test_list_deterministic_ordering(repo: FilesystemRepository):
    """Test 10: list() returns valid investigation IDs sorted deterministically."""
    for inc_id in ["inc_zebra", "inc_apple", "inc_mango"]:
        state = InvestigationState(incident_id=inc_id)
        repo.save(state)

    # Also create a directory without state.json
    (repo.persistence_root / "random_dir").mkdir(parents=True)

    ids = repo.list()
    assert ids == ["inc_apple", "inc_mango", "inc_zebra"]


def test_exists_behaviour(repo: FilesystemRepository, tmp_path: Path):
    """exists() returns True only for genuine state.json, not temporary files."""
    assert not repo.exists("inc_test")

    inv_dir = tmp_path / "inc_test"
    inv_dir.mkdir(parents=True)
    (inv_dir / "state_12345.tmp").write_text("{}", encoding="utf-8")
    # Tmp file is not state.json
    assert not repo.exists("inc_test")

    state = InvestigationState(incident_id="inc_test")
    repo.save(state)
    assert repo.exists("inc_test")


def test_atomic_write_behaviour(repo: FilesystemRepository):
    """Test 11: Writes are performed atomically via temporary file and replace."""
    state = InvestigationState(incident_id="inc_atomic")
    with patch("os.replace", wraps=__import__("os").replace) as mock_replace:
        repo.save(state)
        assert mock_replace.called
        # First arg was a .tmp file, second arg was state.json
        tmp_arg, target_arg = mock_replace.call_args[0]
        assert str(tmp_arg).endswith(".tmp")
        assert str(target_arg).endswith("state.json")


def test_temporary_file_cleanup_on_error(repo: FilesystemRepository):
    """Test 12: Temporary file is cleaned up if atomic replace fails."""
    state = InvestigationState(incident_id="inc_cleanup")

    with patch("os.replace", side_effect=OSError("Disk write error")):
        with pytest.raises(PersistenceError, match="Failed to atomically write state file"):
            repo.save(state)

    inv_dir = repo.persistence_root / "inc_cleanup"
    # No lingering .tmp files
    tmp_files = list(inv_dir.glob("*.tmp"))
    assert tmp_files == []


@pytest.mark.parametrize("bad_id", [
    "../foo",
    r"..\foo",
    "/foo",
    r"C:\foo",
    "foo/bar",
    r"foo\bar",
    "..",
    ".",
    "inc with spaces",
    ":colon_id",
    "",
])
def test_path_traversal_rejection(repo: FilesystemRepository, bad_id: str):
    """Test 13: Path traversal and unsafe ID attempts are strictly rejected."""
    with pytest.raises(PersistenceError):
        repo.load(bad_id)

    with pytest.raises(PersistenceError):
        repo.exists(bad_id)

    # State with bad ID cannot be saved
    try:
        bad_state = InvestigationState(incident_id=bad_id)
        with pytest.raises(PersistenceError):
            repo.save(bad_state)
    except ValueError:
        # Pydantic may also reject empty strings, which is expected
        pass


def test_forbidden_ground_truth_rejection(repo: FilesystemRepository):
    """Test 14: Serialized state referencing forbidden ground_truth.md is rejected."""
    state = InvestigationState(incident_id="inc_gt_leak")
    gt_name = "ground" + "_truth.md"
    state.metadata["forbidden_ref"] = gt_name

    with pytest.raises(PersistenceError, match="forbidden benchmark artefact detected"):
        repo.save(state)


def test_forbidden_baseline_artefact_rejection(repo: FilesystemRepository):
    """Test 15: Serialized state referencing forbidden baseline files is rejected."""
    for forbidden in ["results_" + "baseline.csv", "baseline_" + "summary.json"]:
        state = InvestigationState(incident_id="inc_baseline_leak")
        state.metadata["forbidden_file"] = forbidden
        with pytest.raises(PersistenceError, match="forbidden benchmark artefact detected"):
            repo.save(state)


def test_forbidden_secret_rejection(repo: FilesystemRepository):
    """Defence in depth: Serialized state containing API key secrets is rejected."""
    state = InvestigationState(incident_id="inc_secret_leak")
    secret_key = "GROQ" + "_API_KEY"
    state.metadata["env"] = {secret_key: "gsk_test12345"}

    with pytest.raises(PersistenceError, match="forbidden secret key detected"):
        repo.save(state)


def test_repository_requires_investigation_state_not_dict(repo: FilesystemRepository):
    """Test 16: save() must reject raw dictionaries and require InvestigationState."""
    with pytest.raises(PersistenceError, match="accepts only InvestigationState"):
        repo.save({"incident_id": "fake", "status": "RUNNING"})  # type: ignore
