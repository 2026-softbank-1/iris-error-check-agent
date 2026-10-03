"""Conservative source prefetch hints; never diagnoses or reads local paths."""

import re
from dataclasses import dataclass

from .errors import DiagnosisError
from .source_contracts import safe_source_path


@dataclass(frozen=True)
class DiagnosisSettings:
    mode: str = "standard"

    def __post_init__(self):
        if self.mode not in {"standard", "adaptive", "graph_compact"}:
            raise ValueError("invalid diagnosis mode")

    @classmethod
    def from_config(cls, config):
        try:
            return cls(mode=config.get("AGENT_DIAGNOSIS_MODE") or "standard")
        except ValueError:
            raise DiagnosisError(
                "INVALID_CONFIG",
                "AGENT_DIAGNOSIS_MODE는 standard, adaptive 또는 graph_compact여야 합니다.",
            ) from None


# Start with the observed Node startup incident family. Other languages/error
# families deliberately keep the model-led path until independently evaluated.
MISSING_FILE = re.compile(r"\bError: ENOENT: no such file or directory, open ['\"]([^'\"]+)['\"]")
FRAME = re.compile(
    r"^\s*at (?:[^\s()]+ \()?((?:file://)?[^\s():]+):([1-9][0-9]{0,5})"
    r":[1-9][0-9]{0,5}\)?\s*$"
)
RECOVERY = re.compile(
    r"recover|healthy|health.?check.*(?:pass|200|ok)|ready|listening|startup.*(?:complete|success)",
    re.IGNORECASE,
)
OTHER_FAILURE = re.compile(r"\berror\b|exception|\bfatal\b|\bfailed\b|\bfailure\b", re.IGNORECASE)
ROOTS = ("/usr/src/app/", "/app/", "/workspace/")
REASON = "ENOENT 스택의 첫 애플리케이션 파일을 사전 선택했습니다. 원인 확정 근거는 아닙니다."


def candidate_ranges(bundle):
    """One repeated error and one producer/stream; keep every log for the LLM.

    An absolute container path is mapped only through a known deployment prefix.
    The resulting identifier still must exist in the supplied project archive.
    No basename/suffix guessing, filesystem access, or causal assertion occurs.
    """
    if bundle.context.get("deployment_status") == "succeeded":
        return []
    groups = {(line.source_id, line.stage, line.stream) for line in bundle.lines}
    if len(groups) != 1 or any(RECOVERY.search(line.text) for line in bundle.lines):
        return []
    signatures, anchors, primary = set(), [], []
    awaiting_frame = False
    for line in bundle.lines:
        match = MISSING_FILE.search(line.text)
        if match:
            signatures.add(match.group(1))
            anchors.append(line.id)
            awaiting_frame = True
            continue
        frame = FRAME.fullmatch(line.text)
        if frame and awaiting_frame:
            raw = frame.group(1).removeprefix("file://")
            if "/node_modules/" in raw:
                return []
            if raw.startswith("/"):
                root = next((root for root in ROOTS if raw.startswith(root)), None)
                if root is None:
                    return []
                raw = raw[len(root) :]
            raw = raw.removeprefix("./")
            try:
                safe_source_path(raw)
            except ValueError:
                return []
            if not raw.endswith((".js", ".mjs", ".cjs")):
                return []
            primary.append((raw, int(frame.group(2)), line.id))
            awaiting_frame = False
        elif OTHER_FAILURE.search(line.text):
            # Only a generic Node termination line can accompany this family.
            if not re.search(r"\b(?:Application|application) startup failed\b", line.text):
                return []
    if len(signatures) != 1 or not primary or awaiting_frame:
        return []
    if len({(path, number) for path, number, _ in primary}) != 1:
        return []
    path, number, _ = primary[0]
    if number > 120:
        return []
    return [
        {
            "path": path,
            "start_line": number,
            "end_line": 120,
            "reason": REASON,
            "evidence_ids": list(dict.fromkeys([anchors[0], primary[0][2]])),
        }
    ]


def complete_ranges(index, requests):
    """Require the complete primary file and a direct file-read call.

    The LLM still determines whether the read, missing path and failure are
    related. Complex wrappers/large modules fall back to log-first selection.
    """
    if len(requests) != 1:
        return []
    item = requests[0]
    file = index.get(item["path"])
    if not file or not 1 <= len(file["lines"]) <= 120:
        return []
    if item["start_line"] > len(file["lines"]):
        return []
    if not re.search(r"\breadFileSync\s*\(", "\n".join(file["lines"])):
        return []
    return [{**item, "start_line": 1, "end_line": len(file["lines"])}]


CONCISE_PROMPT = """
[응답 길이 최적화]
기존의 근거·반대 근거·불확실성·조건부 수정·검증·되돌리기 기준은 모두 지킨다.
각 설명은 가능하면 한 문장으로 작성하고 동일한 설명을 여러 필드에 반복하지 않는다.
한 오류의 반복 스택은 하나의 관찰로 묶되 다른 증상과 회복·반대 근거는 생략하지 않는다.
필요한 후보·수정 계획·코드 예시만 작성한다. 항목 수를 채우기 위해 예시를 늘리지 않는다.
단일 증상이면 기본은 관찰 1개, 후보 1개, 다음 확인 1개, 추가 자료 1개, 한계 2개,
수정 계획 1개·수정 예시 1개·검증 1~2개·되돌리기 1개·위험 1개다.
각 항목에 필요한 조건과 구분 기준을 함께 적고, 다른 원인·회복·반대 근거를 다루려면 확장한다.
코드의 올바른 문법, 데이터 보존 조건, 검증 판정 기준과 중요한 위험은 줄이지 않는다.
summary부터 source_findings까지 모든 자연어 설명은 반드시 한국어로 작성한다.
간결함을 위해 중국어·영어로 바꾸지 않는다. 코드·경로·식별자만 원래 표기를 유지한다.
"""

PREFETCH_PROMPT = """
[사전 선택한 소스]
로그의 timestamp는 UTC 시간, sequence는 같은 source_id/stage/stream 안에서의 순서다.
다른 출처의 sequence를 서로 비교하지 않는다. text의 여러 줄은 하나의 로그 이벤트일 수 있다.
서버가 스택에 나타난 첫 애플리케이션 파일을 미리 제공했다. 파일 선택은 원인 판정이 아니다.
전체 로그와 반대 근거를 독립적으로 검토한다. 다른 파일·배포 설정·마운트는 확인하지 않았다.
파일 부재만으로 패키징 누락을 확정하거나 빈 파일·빈 배열로 초기화하도록 권하지 않는다.
추가 파일이나 계약이 필요하면 missing_information과 limitations에 명시하고, 근거가 부족하면
insufficient_evidence로 답한다. 속도 때문에 근거가 없는 원인·안전한 수정안을 만들어내지 않는다.
"""
