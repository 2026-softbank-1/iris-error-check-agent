"""Normalize and redact a full log snapshot without renumbering physical lines."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace

from .contracts import DiagnosisRequest
from .errors import DiagnosisError

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z ]+)?PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
AUTH = re.compile(r"(?im)(\b(?:authorization|proxy-authorization)\s*:\s*)[^\n]+")
COOKIE = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\n]+")
BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
URL_AUTH = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
SECRET_FIELD = re.compile(
    r"""(?ix)(["']?\b(?:[A-Z0-9_]*(?:PASSWORD|PASSWD|API_KEY|APIKEY|SECRET|TOKEN)"""
    r"""|PWD)["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\{\{[A-Z][A-Z0-9_]{0,63}\}\}|[^\s,;&}\]]+)"""
)
KNOWN_TOKEN = re.compile(
    r"\b(?:AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
SIGNAL = re.compile(
    r"error|exception|failed|failure|killed|refused|denied|not found|missing", re.IGNORECASE
)


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL.sub("", ANSI.sub("", text))


def redact(text: str, *, allow_placeholders: bool = False) -> str:
    # Keep all newlines even when an entire multiline credential is removed.
    text = PRIVATE_KEY.sub(lambda m: "\n".join("[REDACTED]" for _ in m[0].split("\n")), text)
    text = AUTH.sub(r"\1[REDACTED]", text)
    text = COOKIE.sub(r"\1[REDACTED]", text)
    text = BEARER.sub(r"\1[REDACTED]", text)
    text = URL_AUTH.sub(r"\1[REDACTED]@", text)

    def field(match: re.Match) -> str:
        value = match[2]
        # Only generated templates can preserve a literal, declared placeholder.
        # Input logs always use the default masking behavior.
        if allow_placeholders and re.fullmatch(r"\{\{[A-Z][A-Z0-9_]{0,63}\}\}", value.strip("\"'")):
            return match[0]
        replacement = "\n".join("[REDACTED]" for _ in value.split("\n"))
        if value.startswith(('"', "'")):
            replacement = value[0] + replacement + value[-1]
        return match[1] + replacement

    text = SECRET_FIELD.sub(field, text)
    return KNOWN_TOKEN.sub("[REDACTED]", text)


def redact_object(value):
    if isinstance(value, str):
        return redact(normalize(value))
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_object(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class EvidenceLine:
    id: str
    chunk_id: str
    source_id: str
    stage: str
    stream: str
    chunk_line: int
    source_line: int | None
    text: str


@dataclass(frozen=True)
class EvidenceBundle:
    context: dict
    lines: tuple[EvidenceLine, ...]
    limitations: tuple[str, ...]
    snapshot_sha256: str

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(line.id for line in self.lines)

    def model_payload(self) -> dict:
        return {
            "context": self.context,
            "logs": [asdict(line) for line in self.lines],
            "candidate_signals": [
                {"evidence_id": line.id, "signal": "possible_failure; not a diagnosis"}
                for line in self.lines
                if SIGNAL.search(line.text)
            ],
            "input_limitations": list(self.limitations),
            "selection_version": "full-input.v1",
        }


def prepare(request: DiagnosisRequest, max_evidence_bytes: int = 16_384) -> EvidenceBundle:
    lines = []
    limitations = []
    for chunk in request.logs:
        normalized = normalize(chunk.text)
        masked = redact(normalized)
        if masked != normalized:
            limitations.append("비밀값을 마스킹했으므로 일부 내용을 확인할 수 없습니다.")
        if not chunk.is_complete:
            limitations.append(f"{chunk.chunk_id}: 백엔드가 수집 범위의 누락을 표시했습니다.")
        physical_lines = masked.split("\n")
        for index, text in enumerate(physical_lines, start=1):
            # A terminal newline is a terminator, not an extra physical line.
            if index == len(physical_lines) and text == "":
                continue
            lines.append(
                EvidenceLine(
                    id=f"EV{len(lines) + 1:06d}",
                    chunk_id=chunk.chunk_id,
                    source_id=chunk.source_id,
                    stage=chunk.stage,
                    stream=chunk.stream,
                    chunk_line=index,
                    source_line=None
                    if chunk.source_line_start is None
                    else chunk.source_line_start + index - 1,
                    text=text,
                )
            )
    if not any(line.text.strip() for line in lines):
        raise DiagnosisError("EMPTY_LOGS", "정규화한 로그에 분석할 내용이 없습니다.")
    if len(lines) > 10_000:
        raise DiagnosisError("INPUT_TOO_LARGE", "로그가 10,000줄을 초과했습니다.")
    bundle = EvidenceBundle(
        context=request.context.model_dump(),
        lines=tuple(lines),
        limitations=tuple(dict.fromkeys(limitations)),
        snapshot_sha256="",
    )
    serialized = json.dumps(bundle.model_payload(), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    payload_size = len(serialized)
    if payload_size > max_evidence_bytes:
        raise DiagnosisError(
            "INPUT_TOO_LARGE", "초기 버전의 로그 입력 예산을 초과했습니다. 관련 구간을 줄여 주세요."
        )
    return replace(bundle, snapshot_sha256=hashlib.sha256(serialized).hexdigest())
