"""Validate advisory changes. Generated snippets are never evaluated or executed."""

import copy
import re

from .errors import DiagnosisError
from .preprocessing import normalize, redact, redact_object

PLACEHOLDER = re.compile(r"\{\{([^{}\n]+)\}\}")


def sanitize_analysis(value):
    value = copy.deepcopy(value)
    snippets = []
    for plan in value["remediation"]["plans"]:
        for change in plan["changes"]:
            snippet = normalize(change["snippet"])
            masked = redact(snippet, allow_placeholders=True)
            if masked != snippet:
                # Masking arbitrary source code can turn it into misleading or invalid code.
                raise DiagnosisError(
                    "UNSAFE_SNIPPET",
                    "수정 예시에 비밀값으로 보이는 내용이 있습니다. 원문 대신 자리표시자를 사용해야 합니다.",
                )
            snippets.append(snippet)
            change["snippet"] = ""
    value = redact_object(value)
    iterator = iter(snippets)
    for plan in value["remediation"]["plans"]:
        for change in plan["changes"]:
            change["snippet"] = next(iterator)
    return value


def validate_remediation(value, bundle, *, known_source_paths=()):
    remediation = value["remediation"]
    plans = remediation["plans"]
    expected = {
        "diagnosed": "proposed",
        "insufficient_evidence": "needs_more_evidence",
        "no_failure_evidence": "not_needed",
    }[value["analysis_status"]]
    if remediation["status"] != expected or bool(plans) != (expected == "proposed"):
        raise DiagnosisError("INVALID_REMEDIATION", "진단 상태와 해결안 제공 상태가 맞지 않습니다.")
    if len({plan["id"] for plan in plans}) != len(plans):
        raise DiagnosisError("INVALID_REFERENCE", "해결안 ID가 중복됐습니다.")
    hypotheses = {h["id"]: h for h in value["hypotheses"]}
    covered = set()
    for plan in plans:
        refs = set(plan["hypothesis_ids"])
        if not refs <= hypotheses.keys():
            raise DiagnosisError(
                "INVALID_REFERENCE", "해결안이 존재하지 않는 원인 후보를 참조했습니다."
            )
        evidence = set(plan["evidence_ids"])
        if not evidence <= bundle.evidence_ids:
            raise DiagnosisError("INVALID_EVIDENCE", "해결안에 제공하지 않은 근거가 포함됐습니다.")
        if not all(evidence & set(hypotheses[ref]["evidence_ids"]) for ref in refs):
            raise DiagnosisError(
                "INVALID_REFERENCE", "해결안과 원인 후보의 근거가 연결되지 않습니다."
            )
        covered.update(refs)
        for change in plan["changes"]:
            if not change["snippet"].strip():
                raise DiagnosisError("INVALID_REMEDIATION", "빈 수정 예시는 허용하지 않습니다.")
            names = [item["name"] for item in change["placeholders"]]
            used = set(PLACEHOLDER.findall(change["snippet"]))
            if len(names) != len(set(names)) or used != set(names):
                raise DiagnosisError(
                    "INVALID_REMEDIATION", "수정 예시의 자리표시자 설명이 일치하지 않습니다."
                )
            if (
                change["target_known"]
                and change["target"] not in known_source_paths
                and not any(change["target"] in line.text for line in bundle.lines)
            ):
                raise DiagnosisError(
                    "INVALID_REMEDIATION", "입력에서 확인되지 않은 수정 대상을 확정했습니다."
                )
    if plans and value["hypotheses"][0]["id"] not in covered:
        raise DiagnosisError("INVALID_REMEDIATION", "우선 원인 후보에 대한 해결안이 없습니다.")
