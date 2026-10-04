"""Bounded log locations, not causal claims or instructions to access local files.

All locations retain EV provenance. Only exact paths inside the supplied project
are read; no basename search, compiler execution, or filesystem probing occurs.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace

from .source_contracts import safe_source_path

MAX_LOCATIONS = 12
MAX_LINE = 1_000_000
ROOTS = ("/usr/src/app/", "/app/", "/workspace/")
EXCLUDED = {"node_modules", "vendor", ".venv", "venv", ".git", "dist", "build"}
EXTENSIONS = (
    "py",
    "pyi",
    "js",
    "mjs",
    "cjs",
    "jsx",
    "ts",
    "tsx",
    "mts",
    "cts",
    "vue",
    "svelte",
    "go",
    "rs",
    "c",
    "h",
    "cc",
    "cpp",
    "hpp",
    "cs",
    "java",
    "kt",
    "kts",
    "rb",
    "php",
    "sh",
    "json",
    "yaml",
    "yml",
    "toml",
    "sql",
    "html",
    "css",
    "scss",
)
PATH = (
    r"(?<![^\s(\"'])(?P<path>(?:file://)?[^\s\"'<>|()\[\],;]{1,256}?\.(?:"
    + "|".join(EXTENSIONS)
    + r"))"
)
COLON = re.compile(PATH + r":(?P<line>\d{1,7})(?::(?P<column>\d{1,7})(?!\d))?(?!:\d)(?=[:\s)]|$)")
PARENS = re.compile(PATH + r"\((?P<line>\d{1,7}),(?P<column>\d{1,7})\)")
WEBPACK = re.compile(r"^ERROR in " + PATH + r" (?P<line>\d{1,7}):(?P<column>\d{1,7})(?!\d)")
PYTHON = re.compile(r"^\s*File [\"\'](?P<path>[^\"\']+)[\"\'], line (?P<line>\d{1,7})")
SHELL = re.compile(PATH + r":\s*line\s+(?P<line>\d{1,7})(?=[:\s]|$)")
EXCEPTION = re.compile(
    r"^(?:(?:[A-Za-z_$][\w.$]*(?:Error|Exception)|Error|Exception)(?:\s*\[[^\]]{1,60}\])?:"
    r"|panic:|fatal error:|error(?:\[[A-Z]\d+\])?:)",
    re.IGNORECASE,
)
DIAGNOSTIC = re.compile(
    r"\b(?:error(?:\s+[A-Z]+\d+)?|fatal error|syntax error|parse error|parsing error)\b"
    r"|\b(?:undefined:|cannot (?:use|find|convert)|expected [\"';})])",
    re.IGNORECASE,
)
WARNING = re.compile(r"^\s*(?:warning|note|info|help)\b", re.IGNORECASE)
RECOVERY = re.compile(
    r"\b(?:recovered|healthy)\b|\bhealth.?check.*(?:pass|200|ok)"
    r"|\b(?:startup|build|compilation)\s+(?:complete|successful|succeeded)"
    r"|\b(?:test|fixture).*\b(?:expected|asserted)\b",
    re.IGNORECASE,
)
LINT_ROW = re.compile(r"^\s*(?P<line>\d{1,7}):(?P<column>\d{1,7})\s+error\b")
PREFIX = re.compile(
    r"^(?:#\d+\s+(?:\d+(?:\.\d+)?\s+)?|npm ERR!\s*|\[(?:ERROR|FATAL|startup-error)\]\s*)",
    re.IGNORECASE,
)
TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\s+"
)
CODE = re.compile(r"\b(?:TS|CS|E)\d{3,5}\b")
LOCATION_REASON = (
    "로그에 보고된 오류·호출 위치의 주변 소스를 사전 선택했습니다. 원인 확정은 아닙니다."
)


@dataclass(frozen=True)
class ErrorLocation:
    path: str
    line: int
    column: int | None
    kind: str
    evidence_ids: tuple[str, ...]
    scope: tuple[str, str, str]
    code: str | None = None
    prefetch: bool = True

    def model_value(self):
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "kind": self.kind,
            "error_code": self.code,
            "evidence_ids": list(self.evidence_ids),
        }


def project_path(raw, root_directory="."):
    """Map only known container roots; project root remains a hard boundary."""
    if raw.startswith("file://") and not raw.startswith("file:///"):
        return None
    raw = raw.removeprefix("file://")
    # Windows diagnostics can use an explicitly known container-equivalent root.
    raw = raw.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", raw):
        raw = raw[2:]
    if raw.startswith("/"):
        prefix = next((p for p in ROOTS if raw.startswith(p)), None)
        if prefix is None:
            return None
        raw = raw[len(prefix) :]
    raw = raw.removeprefix("./")
    try:
        safe_source_path(raw)
    except ValueError:
        return None
    if root_directory not in {None, "", "."} and raw.startswith(root_directory + "/"):
        raw = raw[len(root_directory) + 1 :]
    if any(p.lower() in EXCLUDED for p in raw.split("/")):
        return None
    return raw


def extract_error_locations(bundle, *, root_directory="."):
    """Recognize compiler diagnostics and bounded same-stream exception blocks.

    Python/Node syntax locations can precede the error message, whereas Node,
    Go, Rust and JVM frames commonly follow it. A location without an error
    anchor is never promoted to an error. Warning/note lines are not anchors.
    """
    groups = defaultdict(list)
    for line in bundle.lines:
        groups[(line.source_id, line.stage, line.stream)].append(line)
    found = []
    for scope, lines in groups.items():
        pending, active, heading = [], None, None
        recovered = any(RECOVERY.search(line.text) for line in lines)

        def add(
            match, location_ev, anchor, kind, *, extra_ids=(), scope=scope, recovered=recovered
        ):
            path = project_path(match["path"], root_directory)
            number = int(match["line"])
            column = match.get("column")
            column = int(column) if column is not None else None
            if (
                path is None
                or not 1 <= number <= MAX_LINE
                or (column is not None and not 1 <= column <= MAX_LINE)
            ):
                return
            code = CODE.search(anchor.text)
            found.append(
                ErrorLocation(
                    path,
                    number,
                    column,
                    kind,
                    tuple(dict.fromkeys((location_ev.id, anchor.id, *extra_ids))),
                    scope,
                    code[0] if code else None,
                    # Preserve the existing file-read graph's stricter entry rules.
                    not recovered and "ENOENT" not in anchor.text,
                )
            )

        for pos, line in enumerate(lines):
            text = line.text.strip()
            for _ in range(3):
                text = PREFIX.sub("", TIMESTAMP.sub("", text, count=1), count=1)
            pending = [p for p in pending if pos - p[0] <= 16]
            if active and pos - active[0] > 48:
                active = None
            if text.startswith("Traceback ") or RECOVERY.search(text):
                pending, active, heading = [], None, None
            if WARNING.match(text):
                pending, active = [], None
                continue
            # ESLint stylish format: a full path, followed by line:column rows.
            if project_path(text, root_directory) and re.fullmatch(PATH, text):
                heading = (pos, text, line.id)
                pending, active = [], None
                continue
            lint = LINT_ROW.match(text)
            if lint and heading and pos - heading[0] <= 24:
                add(
                    {**lint.groupdict(), "path": heading[1]},
                    line,
                    line,
                    "diagnostic",
                    extra_ids=(heading[2],),
                )
                continue
            match = (
                PYTHON.match(text)
                or PARENS.search(text)
                or WEBPACK.match(text)
                or SHELL.search(text)
                or COLON.search(text)
            )
            if match:
                values = match.groupdict()
                tail = text[match.end() :].lstrip(": ")
                if WARNING.match(tail):
                    continue
                # On-line compiler diagnostics (tsc, GCC, Go, C#, etc.).
                if DIAGNOSTIC.search(tail) or text.startswith("ERROR in "):
                    add(values, line, line, "diagnostic")
                    pending, active = [], (pos, line)
                elif text.startswith("File "):
                    pending.append((pos, values, line, "traceback_frame"))
                    active = None
                elif text.startswith(("at ", "--> ")) or re.search(r"\s\+0x[0-9a-f]+$", text):
                    if active:
                        kind = "diagnostic" if text.startswith("--> ") else "stack_frame"
                        add(values, line, active[1], kind)
                elif match.start() == 0 and not tail:
                    # node --check: /app/src/app.js:9, code, caret, SyntaxError.
                    pending.append((pos, values, line, "diagnostic"))
                continue
            if EXCEPTION.match(text):
                # Deepest Python frame first; all frames remain observed locations.
                for _, values, location_ev, kind in reversed(pending):
                    if re.match(r"^(?:SyntaxError|IndentationError|TabError):", text):
                        kind = "diagnostic"
                    add(values, location_ev, line, kind)
                pending, active = [], (pos, line)
            elif text.startswith(("INFO ", "DEBUG ", "WARN ")):
                active = None
    # Prefer explicit diagnostics, preserve frame order, merge repeated occurrences
    # only within one producer/stage/stream. Never merge unrelated traceback tails.
    unique = {}
    for item in sorted(found, key=lambda x: x.kind != "diagnostic"):
        key = (item.path, item.line, item.column, item.kind, item.scope)
        if key in unique:
            previous = unique[key]
            unique[key] = replace(
                previous,
                evidence_ids=tuple(dict.fromkeys(previous.evidence_ids + item.evidence_ids))[:8],
            )
        else:
            unique[key] = item
    return list(unique.values())[:MAX_LOCATIONS]


def location_payload(locations, *, max_bytes=3072):
    values = []
    for item in locations:
        candidate = values + [item.model_value()]
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > max_bytes:
            break
        values = candidate
    return values


def location_ranges(locations, *, prefetch=True):
    """At most three exact files, one bounded context window per file."""
    ranges = {}
    for item in locations:
        if prefetch and not item.prefetch:
            continue
        if item.path not in ranges:
            if len(ranges) == 3:
                continue
            ranges[item.path] = {
                "path": item.path,
                "start_line": max(1, item.line - 8),
                "end_line": item.line + 8,
                "reason": LOCATION_REASON,
                "evidence_ids": list(item.evidence_ids),
            }
        else:
            r = ranges[item.path]
            start, end = (
                min(r["start_line"], max(1, item.line - 8)),
                max(r["end_line"], item.line + 8),
            )
            if end - start < 60:
                r.update(
                    start_line=start,
                    end_line=end,
                    evidence_ids=list(dict.fromkeys(r["evidence_ids"] + list(item.evidence_ids)))[
                        :8
                    ],
                )
    return list(ranges.values())


def fit_location_ranges(index, requests, locations):
    """Do not silently turn an out-of-range diagnostic into unrelated source."""
    fitted, limits = [], []
    for request in requests:
        path = request["path"]
        file = index.get(path)
        count = len(file["lines"]) if file else 0
        anchors = [
            x
            for x in locations
            if x.path == path and request["start_line"] <= x.line <= request["end_line"]
        ]
        if (
            not count
            or not anchors
            or not all(
                x.line <= count or (x.kind == "diagnostic" and x.line == count + 1) for x in anchors
            )
        ):
            limits.append(f"{path}: 오류 위치가 제공된 소스 범위에 없어 사전 조회를 보류했습니다.")
            continue
        if any(x.line == count + 1 for x in anchors):
            limits.append(
                f"{path}: 오류가 파일 끝 다음 줄을 가리켜 마지막 실제 코드 줄까지 확인합니다. "
                "보고된 EOF 위치에 가상의 SC 근거를 생성하지 않습니다."
            )
        fitted.append({**request, "end_line": min(request["end_line"], count)})
    return fitted, limits


LOCATION_PROMPT = """
[로그에서 추출한 오류 위치]
error_locations는 로그에 실제로 보고된 파일·행·열과 EV 참조다. 원인 정답이 아니다.
diagnostic은 도구가 오류를 보고한 위치이고 stack_frame/traceback_frame은 호출 위치다.
파서가 멈춘 줄보다 앞에서 괄호·따옴표·구분자가 빠졌을 수 있으므로 주변 SC 소스와 대조한다.
세미콜론 생략은 언어·문맥에 따라 유효하다. 누락 자체만으로 오류라고 판단하지 않는다.
경고·복구·예상된 테스트 실패와 실제 장애를 구분하고 여러 오류·출처를 함께 검토한다.
파일·줄이 소스 스냅샷과 다르거나 생성 코드이면 그 한계를 설명한다. 원본 위치를 추측하지 않는다.
확인한 코드 원인은 source_findings에 EV·SC·실제 줄 범위를 연결하고, 수정 예시에도 해당
파일·실제 문맥을 반영한다. 컴파일러·린터를 실행하거나 수정의 통과를 검증했다고 주장하지 않는다.
소스 미제공 또는 예산으로 제외된 위치는 추가 확인 대상으로 남긴다.
"""

LOCATION_PREFETCH_PROMPT = """
[오류 위치에 따른 사전 소스 조회]
서버가 같은 프로젝트의 소스에서 보고된 파일·행 주변만 선택했다. 전체 파일을 읽은 것은 아니다.
파일·행·열은 오류 도구가 보고한 위치이며 원인 판정이 아니다. 앞선 구문, 호출자·피호출자,
다른 오류와 회복 근거를 함께 검토한다. 원인과 수정 대상을 좁힐 수 없으면 추가 자료를 요청한다.
"""
