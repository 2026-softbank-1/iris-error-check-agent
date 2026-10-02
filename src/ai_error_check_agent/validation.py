import json
from importlib.resources import files

from jsonschema import Draft202012Validator

from .errors import DiagnosisError
from .preprocessing import EvidenceBundle
from .remediation import sanitize_analysis, validate_remediation


def load_schema() -> dict:
    return json.loads(
        files("ai_error_check_agent").joinpath("agent/analysis.schema.json").read_text("utf-8")
    )


def load_prompt() -> str:
    return files("ai_error_check_agent").joinpath("agent/diagnosis_prompt.md").read_text("utf-8")


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def strict_json(text: str):
    def reject_constant(_):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=reject_constant)


def validate_analysis(text: str, bundle: EvidenceBundle) -> dict:
    try:
        value = strict_json(text)
    except (ValueError, RecursionError):
        raise DiagnosisError("INVALID_JSON", "모델 응답이 단일 JSON 객체가 아닙니다.") from None
    validator = Draft202012Validator(load_schema())
    if not validator.is_valid(value):
        raise DiagnosisError("INVALID_SCHEMA", "모델 응답이 진단 결과 규격을 만족하지 않습니다.")
    value = sanitize_analysis(value)
    if not validator.is_valid(value):
        raise DiagnosisError("INVALID_SCHEMA", "마스킹한 응답이 결과 규격을 만족하지 않습니다.")

    observations = {item["id"]: item for item in value["observations"]}
    hypotheses = {item["id"]: item for item in value["hypotheses"]}
    checks = {item["id"] for item in value["next_checks"]}
    if (
        len(observations) != len(value["observations"])
        or len(hypotheses) != len(value["hypotheses"])
        or len(checks) != len(value["next_checks"])
    ):
        raise DiagnosisError("INVALID_REFERENCE", "관찰·후보·확인 ID가 중복됐습니다.")

    for item in [*observations.values(), *hypotheses.values()]:
        cited = set(item["evidence_ids"]) | set(item.get("counter_evidence_ids", []))
        if not cited <= bundle.evidence_ids:
            raise DiagnosisError("INVALID_EVIDENCE", "모델에 제공하지 않은 근거 ID가 포함됐습니다.")

    for hypothesis in hypotheses.values():
        if not set(hypothesis["observation_ids"]) <= observations.keys():
            raise DiagnosisError("INVALID_REFERENCE", "존재하지 않는 관찰을 참조했습니다.")
        failures = [
            observations[ref]
            for ref in hypothesis["observation_ids"]
            if observations[ref]["kind"] == "failure"
        ]
        if not any(set(obs["evidence_ids"]) & set(hypothesis["evidence_ids"]) for obs in failures):
            raise DiagnosisError(
                "INVALID_REFERENCE", "후보와 실패 관찰의 근거가 연결되지 않습니다."
            )
        if set(hypothesis["evidence_ids"]) & set(hypothesis["counter_evidence_ids"]):
            raise DiagnosisError("INVALID_EVIDENCE", "지지 근거와 반대 근거가 중복됐습니다.")
    for check in value["next_checks"]:
        if not set(check["hypothesis_ids"]) <= hypotheses.keys():
            raise DiagnosisError("INVALID_REFERENCE", "존재하지 않는 후보를 참조했습니다.")

    status = value["analysis_status"]
    failures = [obs for obs in observations.values() if obs["kind"] == "failure"]
    if status == "diagnosed":
        valid = bool(failures and hypotheses and checks)
    elif status == "insufficient_evidence":
        valid = not hypotheses and bool(
            value["missing_information"] and checks and value["limitations"]
        )
    else:
        valid = not hypotheses and not failures and bool(value["limitations"])
        if bundle.context["deployment_status"] == "failed":
            valid = valid and bool(value["missing_information"])
    if not valid:
        raise DiagnosisError("INVALID_STATE", "진단 상태와 결과 내용의 필수 조건이 맞지 않습니다.")
    validate_remediation(value, bundle)
    return value
