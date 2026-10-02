import asyncio
import json
from pathlib import Path

import pytest

from ai_error_check_agent.contracts import DiagnosisRequest
from ai_error_check_agent.evaluate import evaluate

MANIFEST = Path(__file__).resolve().parents[1] / "evaluation/development.json"


def test_labels_not_sent_and_all_repetitions_counted(monkeypatch, profile):
    seen = []

    async def fake_live(request, runtime_profile):
        assert isinstance(request, DiagnosisRequest)
        assert "expected_status" not in request.model_dump()
        assert runtime_profile is profile
        seen.append(request)
        return {
            "analysis": {"analysis_status": "insufficient_evidence"},
            "job_status": "succeeded",
            "execution": {"runtime_reusable": True, "elapsed_ms": 10},
        }

    monkeypatch.setattr("ai_error_check_agent.evaluate.run_live", fake_live)
    report = asyncio.run(evaluate(MANIFEST, profile, 3))
    assert len(seen) == report["completed_runs"] == report["planned_runs"] == 9
    assert report["dataset_kind"] == "synthetic_development_only"
    assert report["metrics"]["status_accuracy_on_completed_runs"] == 1 / 3


def test_cleanup_failure_stops_remaining_runs(monkeypatch, profile):
    async def fake_live(request, runtime_profile):
        return {
            "analysis": None,
            "job_status": "timed_out",
            "execution": {"runtime_reusable": False, "elapsed_ms": 30},
        }

    monkeypatch.setattr("ai_error_check_agent.evaluate.run_live", fake_live)
    report = asyncio.run(evaluate(MANIFEST, profile, 3))
    assert report["planned_runs"] == 9
    assert report["completed_runs"] == 1
    assert report["stopped_for_runtime_cleanup"] is True
    assert report["metrics"]["execution_errors"] == 1
    assert report["metrics"]["status_accuracy_on_completed_runs"] == 0


@pytest.mark.parametrize("accuracy,expected_exit", [(0.0, 1), (0.5, 1), (1.0, 0)])
def test_cli_exit_status_reflects_diagnosis_accuracy(
    monkeypatch, tmp_path, accuracy, expected_exit
):
    from ai_error_check_agent.evaluate import main

    async def fake_evaluate(*args, **kwargs):
        return {
            "stopped_for_runtime_cleanup": False,
            "metrics": {
                "execution_errors": 0,
                "status_accuracy_on_completed_runs": accuracy,
            },
        }

    monkeypatch.setattr("ai_error_check_agent.evaluate.evaluate", fake_evaluate)
    output = tmp_path / "evaluation.json"
    assert main(["--manifest", str(MANIFEST), "--output", str(output)]) == expected_exit
    assert (
        json.loads(output.read_text("utf-8"))["metrics"]["status_accuracy_on_completed_runs"]
        == accuracy
    )
