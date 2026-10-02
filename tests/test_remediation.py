import copy
import json

import pytest

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.preprocessing import prepare, redact
from ai_error_check_agent.validation import validate_analysis


def check(data, request):
    return validate_analysis(json.dumps(data), prepare(request))


def test_conditional_solution_has_change_verification_and_rollback(
    analysis_data, diagnosis_request
):
    result = check(analysis_data, diagnosis_request)
    plan = result["remediation"]["plans"][0]
    assert plan["hypothesis_ids"] == ["H1"]
    assert plan["changes"][0]["snippet"] == "DATABASE_URL={{DATABASE_URL}}"
    assert plan["verification"][0]["expected_result"]
    assert plan["apply_when"] and plan["rollback"] and plan["risks"]


@pytest.mark.parametrize("field", ["apply_when", "changes", "verification", "rollback", "risks"])
def test_incomplete_solution_rejected(analysis_data, diagnosis_request, field):
    analysis_data["remediation"]["plans"][0][field] = []
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda p: p.update(hypothesis_ids=["H3"]), "INVALID_REFERENCE"),
        (lambda p: p.update(evidence_ids=["EV999999"]), "INVALID_EVIDENCE"),
        (
            lambda p: p["changes"][0].update(target="/invented/app.py", target_known=True),
            "INVALID_REMEDIATION",
        ),
        (lambda p: p["changes"][0].update(snippet_kind="applied"), "INVALID_SCHEMA"),
        (lambda p: p["changes"][0].update(snippet=" "), "INVALID_REMEDIATION"),
        (lambda p: p["changes"][0].update(placeholders=[]), "INVALID_REMEDIATION"),
        (
            lambda p: p["changes"][0].update(snippet="DATABASE_URL={{UNDECLARED}}"),
            "INVALID_REMEDIATION",
        ),
        (
            lambda p: p["changes"][0]["placeholders"].append(p["changes"][0]["placeholders"][0]),
            "INVALID_REMEDIATION",
        ),
    ],
)
def test_solution_references_and_templates_are_checked(
    analysis_data, diagnosis_request, mutation, code
):
    mutation(analysis_data["remediation"]["plans"][0])
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == code


def test_solution_requires_shared_evidence(analysis_data, diagnosis_request):
    analysis_data["hypotheses"][0]["evidence_ids"] = ["EV000001"]
    analysis_data["remediation"]["plans"][0]["evidence_ids"] = ["EV000002"]
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REFERENCE"


def test_primary_cause_must_have_a_solution(analysis_data, diagnosis_request):
    second = copy.deepcopy(analysis_data["hypotheses"][0])
    second["id"] = "H2"
    analysis_data["hypotheses"].append(second)
    analysis_data["remediation"]["plans"][0]["hypothesis_ids"] = ["H2"]
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REMEDIATION"


@pytest.mark.parametrize(
    "diagnostic_status,remediation_status",
    [
        ("insufficient_evidence", "needs_more_evidence"),
        ("no_failure_evidence", "not_needed"),
    ],
)
def test_uncertain_or_recovered_diagnoses_have_no_changes(
    analysis_data, diagnosis_request, diagnostic_status, remediation_status
):
    analysis_data.update(analysis_status=diagnostic_status, hypotheses=[], observations=[])
    analysis_data["next_checks"][0]["hypothesis_ids"] = []
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REMEDIATION"
    analysis_data["remediation"] = {
        "status": remediation_status,
        "reason": "필요한 근거 또는 현재 상태를 먼저 확인합니다.",
        "plans": [],
    }
    assert check(analysis_data, diagnosis_request)["remediation"]["plans"] == []


def test_diagnosed_cannot_omit_solution(analysis_data, diagnosis_request):
    analysis_data["remediation"]["plans"] = []
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REMEDIATION"


def test_secret_placeholder_preserved_only_in_generated_snippet(analysis_data, diagnosis_request):
    change = analysis_data["remediation"]["plans"][0]["changes"][0]
    change.update(
        snippet='API_KEY="{{SERVICE_KEY}}"',
        placeholders=[{"name": "SERVICE_KEY", "description": "비밀값 저장소의 값으로 대체"}],
    )
    assert (
        check(analysis_data, diagnosis_request)["remediation"]["plans"][0]["changes"][0]["snippet"]
        == 'API_KEY="{{SERVICE_KEY}}"'
    )
    assert "SERVICE_KEY" not in redact('API_KEY="{{SERVICE_KEY}}"')


def test_raw_secret_in_code_is_rejected_without_broken_replacement(
    analysis_data, diagnosis_request
):
    change = analysis_data["remediation"]["plans"][0]["changes"][0]
    change.update(snippet='API_KEY="synthetic-private-value"', placeholders=[])
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "UNSAFE_SNIPPET"
    assert "synthetic-private-value" not in str(error.value)


def test_advisory_code_is_never_executed(analysis_data, diagnosis_request, tmp_path):
    target = tmp_path / "must-not-exist.txt"
    snippet = f"from pathlib import Path\nPath({str(target)!r}).write_text('unexpected')\n"
    change = analysis_data["remediation"]["plans"][0]["changes"][0]
    change.update(
        kind="code",
        language="python",
        snippet=snippet,
        placeholders=[],
        target="적용 위치 확인 필요",
        target_known=False,
    )
    output = check(analysis_data, diagnosis_request)
    assert output["remediation"]["plans"][0]["changes"][0]["snippet"] == snippet
    assert not target.exists()
