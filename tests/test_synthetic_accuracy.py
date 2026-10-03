import asyncio
import copy
import importlib.util
import io
import json
import tarfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_source_analysis import factory, selection

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "synthetic_accuracy", ROOT / "evaluation/run_synthetic_accuracy.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.fixture
def dataset():
    return evaluation.load_dataset(ROOT / "evaluation/synthetic_incidents.v1.json")[0]


def result_for(case):
    return {
        "job_status": "succeeded",
        "remediation_execution": "not_executed",
        "analysis": {"analysis_status": case["gold"]["status"], "hypotheses": []},
        "source_analysis": {},
        "evidence": [],
    }


def test_dataset_has_controls_and_reproduced_faults(dataset):
    cases = dataset["cases"]
    assert Counter(c["gold"]["status"] for c in cases) == {
        "diagnosed": 12,
        "no_failure_evidence": 4,
        "insufficient_evidence": 4,
    }
    reproduced = [c for c in cases if c["provenance"]["kind"] == "local_python_fault_injection"]
    assert len(reproduced) == 4
    assert all(
        c["provenance"]["broken_exit"] != 0 and c["provenance"]["fixed_exit"] == 0
        for c in reproduced
    )
    assert all("gold" not in c["payload"] and "name" not in c["payload"] for c in cases)


def test_absolute_temporary_path_artifact_is_rejected_before_dispatch(tmp_path, dataset):
    broken = copy.deepcopy(dataset)
    logs = broken["cases"][0]["payload"]["data"]["logs"]
    logs[1]["text"] = logs[1]["text"].replace('File "src/app.py"', 'File "/privatesrc/app.py"')
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="project-relative path"):
        evaluation.load_dataset(path)


def test_graph_count_uses_worker_report_fields(dataset):
    case = dataset["cases"][12]
    row = {
        "case_id": case["id"],
        "grade": evaluation.grade(case, result_for(case), 200),
        "execution": {},
        "http_elapsed_ms": 10,
        "knowledge": {"stats": {"recorded": 1}, "report": {"conforms": True}},
    }
    report = evaluation.summarize(
        dataset["cases"], [row], dataset_sha256="frozen", model="test", knowledge_mode="shadow"
    )
    assert report["graphs"] == {"recorded": 1, "conforms": 1}


def test_wrong_archive_root_is_rejected_before_paid_calls(monkeypatch):
    def broken_archive(case):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for file in case["source_files"]:
                body = file["content"].encode()
                member = tarfile.TarInfo("eval-repo-abcdef/" + file["path"])
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        return buffer.getvalue()

    monkeypatch.setattr(evaluation, "fixture_archive", broken_archive)
    with pytest.raises(ValueError, match="does not expose"):
        evaluation.load_dataset(ROOT / "evaluation/synthetic_incidents.v1.json")


def test_root_score_requires_specific_cause_and_primary_evidence(dataset):
    case = dataset["cases"][4]
    result = result_for(case)
    result["analysis"]["hypotheses"] = [
        {
            "id": "H1",
            "category": "configuration",
            "statement": "DATABASE_URL 설정 누락으로 기동 실패",
            "evidence_ids": ["EV1"],
        }
    ]
    result["evidence"] = [
        {"id": "EV1", "text": "ERROR Missing required configuration: DATABASE_URL"}
    ]
    assert evaluation.grade(case, result, 200)["root_cause_correct"] is True
    result["analysis"]["hypotheses"][0]["statement"] = "REDIS_URL 설정 누락으로 실패"
    assert evaluation.grade(case, result, 200)["root_cause_correct"] is False
    result["analysis"]["hypotheses"][0]["statement"] = "DATABASE_URL 설정 누락"
    result["analysis"]["hypotheses"][0]["evidence_ids"] = ["EV2"]
    assert evaluation.grade(case, result, 200)["root_cause_correct"] is False
    assert evaluation.grade(case, result, 503)["root_cause_correct"] is False


def test_source_hit_requires_fault_line_and_primary_hypothesis(dataset):
    case = dataset["cases"][1]
    result = result_for(case)
    result["analysis"]["hypotheses"] = [
        {
            "id": "H1",
            "category": "configuration",
            "statement": "DATABASE_URl 이름 오타",
            "evidence_ids": ["EV1"],
        }
    ]
    result["evidence"] = [{"id": "EV1", "text": "KeyError: DATABASE_URl"}]
    result["source_analysis"] = {
        "findings": [
            {
                "path": "src/config.py",
                "start_line": 1,
                "end_line": 4,
                "source_evidence_ids": ["SC4"],
                "hypothesis_ids": ["H1"],
            }
        ],
        "evidence": [{"id": "SC4", "path": "src/config.py", "line": 4}],
    }
    assert evaluation.grade(case, result, 200)["source_location_hit"] is True
    result["source_analysis"]["findings"][0]["hypothesis_ids"] = ["H2"]
    assert evaluation.grade(case, result, 200)["source_location_hit"] is False
    result["source_analysis"]["findings"][0]["hypothesis_ids"] = ["H1"]
    result["source_analysis"]["evidence"][0]["line"] = 1
    assert evaluation.grade(case, result, 200)["source_location_hit"] is False


def test_failed_execution_is_not_hidden_by_control_metrics(dataset):
    cases = [dataset["cases"][4], dataset["cases"][12], dataset["cases"][16]]
    rows = []
    for case in cases:
        result = result_for(case)
        result["analysis"]["analysis_status"] = "diagnosed"
        rows.append(
            {
                "case_id": case["id"],
                "grade": evaluation.grade(case, result, 200),
                "execution": {},
                "http_elapsed_ms": 10,
            }
        )
    report = evaluation.summarize(
        dataset["cases"], rows, dataset_sha256="frozen", model="test", knowledge_mode="off"
    )
    assert report["metrics"]["root_cause_accuracy"]["denominator"] == 1
    assert report["metrics"]["healthy_false_positive_rate"]["rate"] == 1
    assert report["metrics"]["insufficient_overclaim_rate"]["rate"] == 1
    assert report["coverage"]["rate"] == 3 / 20
    rows[0]["grade"] = evaluation.grade(cases[0], {}, 500)
    report = evaluation.summarize(
        dataset["cases"], rows, dataset_sha256="frozen", model="test", knowledge_mode="off"
    )
    assert report["execution_errors"] == 1
    assert report["metrics"]["root_cause_accuracy"]["denominator"] == 1
    assert report["status_confusion"]["diagnosed"]["execution_error"] == 1


def test_wilson_interval_and_empty_denominator():
    assert evaluation.ratio(0, 0)["rate"] is None
    assert evaluation.ratio(0, 0)["wilson_95"] is None
    perfect = evaluation.ratio(12, 12)
    assert perfect["rate"] == 1
    assert 0.75 < perfect["wilson_95"][0] < 0.76
    assert perfect["wilson_95"][1] == 1


def test_actual_app_never_receives_gold_labels(tmp_path, dataset, analysis_data):
    case = copy.deepcopy(dataset["cases"][4])
    case["gold"]["review_note"] = "GOLD_CANARY_NOT_FOR_MODEL"
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({"version": "synthetic-incidents.v1", "cases": [case]}))
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=not-a-real-provider-key\nLLM_MODEL=test-model\n")
    create, seen, _ = factory([selection(analysis_data, False)])
    args = SimpleNamespace(
        dataset=manifest,
        limit=0,
        env_file=env,
        output_dir=tmp_path / "results",
        knowledge_mode="off",
    )
    assert asyncio.run(evaluation.evaluate(args, runtime_factory=create)) == 0
    dispatched = json.dumps(seen)
    assert "GOLD_CANARY_NOT_FOR_MODEL" not in dispatched
    assert "statement_pattern_groups" not in dispatched
    assert "critical_log_patterns" not in dispatched
    assert "source_fault" not in dispatched
    summary = json.loads((args.output_dir / "summary.json").read_text())
    assert summary["completed"] == 1
    assert summary["graphs"]["recorded"] == 0
