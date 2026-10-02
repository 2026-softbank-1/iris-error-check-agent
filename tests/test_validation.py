import json

import pytest
from jsonschema import Draft202012Validator

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.preprocessing import prepare
from ai_error_check_agent.validation import load_schema, strict_json, validate_analysis


def check(data, request):
    return validate_analysis(json.dumps(data), prepare(request))


def test_design_schema_and_positive_result(analysis_data, diagnosis_request):
    Draft202012Validator.check_schema(load_schema())
    assert check(analysis_data, diagnosis_request)["analysis_status"] == "diagnosed"


@pytest.mark.parametrize("ref", ["EV999999", "EV000003"])
def test_nonexistent_or_unseen_evidence_rejected(analysis_data, diagnosis_request, ref):
    analysis_data["hypotheses"][0]["evidence_ids"] = [ref]
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["observations"].append(d["observations"][0]),
        lambda d: d["hypotheses"][0].update(observation_ids=["O6"]),
        lambda d: d["observations"][0].update(kind="context"),
        lambda d: d["next_checks"][0].update(hypothesis_ids=["H3"]),
    ],
)
def test_cross_reference_validation(analysis_data, diagnosis_request, mutation):
    mutation(analysis_data)
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REFERENCE"


def test_disjoint_failure_evidence_rejected(analysis_data, diagnosis_request):
    analysis_data["observations"][0]["evidence_ids"] = ["EV000001"]
    analysis_data["hypotheses"][0]["evidence_ids"] = ["EV000002"]
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_REFERENCE"


def test_overlapping_counter_evidence_rejected(analysis_data, diagnosis_request):
    analysis_data["hypotheses"][0]["counter_evidence_ids"] = ["EV000001"]
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_EVIDENCE"


@pytest.mark.parametrize("status", ["insufficient_evidence", "no_failure_evidence"])
def test_non_diagnosed_status_has_no_candidates(analysis_data, diagnosis_request, status):
    analysis_data["analysis_status"] = status
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_STATE"
    analysis_data["hypotheses"] = []
    analysis_data["observations"] = []
    analysis_data["next_checks"][0]["hypothesis_ids"] = []
    analysis_data["remediation"] = {
        "status": "needs_more_evidence" if status == "insufficient_evidence" else "not_needed",
        "reason": "추가 근거가 필요하거나 현재 실패 근거가 없습니다.",
        "plans": [],
    }
    assert check(analysis_data, diagnosis_request)["analysis_status"] == status


def test_no_failure_evidence_still_requests_failed_deployment_logs(
    analysis_data, diagnosis_request
):
    analysis_data.update(
        analysis_status="no_failure_evidence",
        hypotheses=[],
        observations=[],
        next_checks=[],
        missing_information=[],
    )
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_STATE"


@pytest.mark.parametrize("text", ["```json\n{}\n```", '{"x":1,"x":2}', '{"x":NaN}', "{} {}"])
def test_non_strict_json_rejected(text, diagnosis_request):
    with pytest.raises(DiagnosisError) as error:
        validate_analysis(text, prepare(diagnosis_request))
    assert error.value.code == "INVALID_JSON"


def test_extra_field_and_unsupported_confidence_rejected(analysis_data, diagnosis_request):
    analysis_data["hypotheses"][0]["confidence"] = 0.99
    with pytest.raises(DiagnosisError) as error:
        check(analysis_data, diagnosis_request)
    assert error.value.code == "INVALID_SCHEMA"


def test_response_secret_redaction(analysis_data, diagnosis_request):
    analysis_data["summary"] = "API_KEY=made-up-secret"
    result = check(analysis_data, diagnosis_request)
    assert "made-up-secret" not in json.dumps(result)


def test_json_decoder_rejects_duplicate_keys():
    with pytest.raises(ValueError):
        strict_json('{"logs": [], "logs": ["private"]}')
