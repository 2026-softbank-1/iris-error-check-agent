"""Parser/routing contracts with synthetic logs, not LLM accuracy scores."""

import asyncio
import copy
import json
import traceback

import httpx
import pytest
from test_api import send
from test_backend_api import envelope, tarball
from test_source_analysis import factory, selection, source_response

from ai_error_check_agent.api import create_app
from ai_error_check_agent.contracts import DiagnosisRequest
from ai_error_check_agent.diagnosis_routing import DiagnosisSettings
from ai_error_check_agent.error_locations import (
    extract_error_locations,
    fit_location_ranges,
    location_payload,
    location_ranges,
    project_path,
)
from ai_error_check_agent.preprocessing import prepare
from ai_error_check_agent.source_analysis import diagnose_with_source, with_error_locations
from ai_error_check_agent.source_contracts import DiagnoseAPIRequest, DiagnoseAPIResult


@pytest.fixture
def analysis_data(analysis_data):
    # A single diagnostic line is sufficient input; keep the mock's references
    # valid without pretending the mock measures semantic diagnosis quality.
    value = copy.deepcopy(analysis_data)
    for item in [*value["observations"], *value["hypotheses"], *value["remediation"]["plans"]]:
        item["evidence_ids"] = ["EV000001"]
    value["hypotheses"][0]["category"] = "build_compile"
    value["remediation"]["plans"][0]["changes"][0]["target_known"] = False
    return value


def bundle(request_data, text):
    request = copy.deepcopy(request_data)
    request["logs"][0]["text"] = text
    return prepare(DiagnosisRequest.model_validate_json(json.dumps(request)))


CASES = [
    ("tsc", "src/app.ts(150,7): error TS1005: ';' expected.", "src/app.ts", 150, 7),
    (
        "gcc",
        "/workspace/src/main.c:9:12: error: expected ';' before '}' token",
        "src/main.c",
        9,
        12,
    ),
    ("go", "./main.go:24:5: undefined: missingName", "main.go", 24, 5),
    ("csharp", "/app/Program.cs(7,9): error CS1002: ; expected", "Program.cs", 7, 9),
    ("esbuild", "src/App.jsx:8:13: ERROR: Expected '}'", "src/App.jsx", 8, 13),
    ("rust", "error[E0308]: mismatched types\n --> src/main.rs:16:5\n |", "src/main.rs", 16, 5),
    (
        "node-syntax",
        "file:///app/src/server.js:12\nconst x = ;\n          ^\nSyntaxError: Unexpected token ';'",
        "src/server.js",
        12,
        None,
    ),
    (
        "node-runtime",
        (
            "TypeError: Cannot read properties of undefined\n"
            "    at render (/app/src/app.js:200:18)\n    at main (/app/src/main.js:9:1)"
        ),
        "src/app.js",
        200,
        18,
    ),
    (
        "python-syntax",
        (
            '  File "/app/src/app.py", line 3\n    def boot()\n'
            "              ^\nSyntaxError: expected ':'"
        ),
        "src/app.py",
        3,
        None,
    ),
    (
        "python-runtime",
        (
            "Traceback (most recent call last):\n"
            '  File "/app/src/main.py", line 5, in main\n    boot()\n'
            '  File "/app/src/boot.py", line 40, in boot\n    int(value)\n'
            "ValueError: invalid literal for int()"
        ),
        "src/boot.py",
        40,
        None,
    ),
    (
        "go-panic",
        (
            "panic: runtime error: index out of range\n\n"
            "goroutine 1 [running]:\nmain.main()\n\t/app/main.go:6 +0x48"
        ),
        "main.go",
        6,
        None,
    ),
    (
        "eslint",
        (
            "/app/src/app.ts\n  5:8  error  Missing semicolon  semi\n"
            "  7:1  warning  Unused name  no-unused-vars"
        ),
        "src/app.ts",
        5,
        8,
    ),
    (
        "bash",
        "/app/scripts/start.sh: line 9: syntax error near unexpected token `fi'",
        "scripts/start.sh",
        9,
        None,
    ),
    (
        "docker-build-prefix",
        "#12 3.521 src/main.ts(10,4): error TS2322: type mismatch",
        "src/main.ts",
        10,
        4,
    ),
    ("npm-prefix", "npm ERR! src/app.ts(13,9): error TS1005: ')' expected", "src/app.ts", 13, 9),
    ("windows-root", r"C:\workspace\src\App.cs(8,4): error CS1002: ; expected", "src/App.cs", 8, 4),
    (
        "webpack",
        "ERROR in ./src/App.tsx 7:8\nModule parse failed: Unexpected token",
        "src/App.tsx",
        7,
        8,
    ),
]


@pytest.mark.parametrize("name,text,path,line,column", CASES, ids=[c[0] for c in CASES])
def test_reported_locations_keep_coordinates_and_provenance(
    request_data, name, text, path, line, column
):
    b = bundle(request_data, text)
    found = extract_error_locations(b)
    assert found, name
    first = found[0]
    assert (first.path, first.line, first.column) == (path, line, column)
    assert set(first.evidence_ids) <= b.evidence_ids
    assert all(item.prefetch for item in found)


def test_actual_python_parser_output_is_located_without_running_code(request_data):
    try:
        compile("def startup()\n    return 1\n", "/app/src/app.py", "exec")
    except SyntaxError as exc:
        text = "".join(traceback.format_exception_only(type(exc), exc))
    found = extract_error_locations(bundle(request_data, text))
    assert [(x.path, x.line) for x in found] == [("src/app.py", 1)]


@pytest.mark.parametrize(
    "text",
    [
        "INFO checking src/app.ts:12:3",
        "src/app.ts:12:3: warning: expected style differs",
        "src/app.ts:12:3: note: previous error here",
        "const x = 1\nconsole.log(x)\nINFO build succeeded",
        "    at main (/app/src/app.js:3:4)",
        '  File "/app/src/app.py", line 2\nINFO normal trace',
        "src/app.js:3\nconst x = 1",
        "https://host/src/app.ts:2:4: error TS1005: ';' expected",
        "file://host/app/src/app.ts:2:4: error TS1005: ';' expected",
        "src/app.ts:0:1: error TS1005: test",
        "src/app.ts:5:0: error TS1005: test",
        "src/app.ts:1000001:1: error TS1005: test",
        "src/app.ts:5:1000001: error TS1005: test",
    ],
)
def test_non_failures_or_invalid_coordinates_are_not_promoted(request_data, text):
    assert extract_error_locations(bundle(request_data, text)) == []


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/app.py",
        "../../src/app.ts",
        "/app/../app.js",
        "/app/.env",
        "/app/node_modules/foo/index.js",
        "/app/dist/index.js",
        "/tmp/src/app.py",
        "src/secret.key",
        "src/sk-abcdefghijklmnopqrstuvwxyz123456.js",
    ],
)
def test_no_unsafe_dependency_generated_or_unknown_paths(raw):
    assert project_path(raw) is None


def test_same_file_name_is_not_guessed():
    assert project_path("main.py") == "main.py"
    assert project_path("/srv/main.py") is None
    assert project_path("/app/apps/api/src/app.py", "apps/api") == "src/app.py"


def test_different_streams_do_not_join_python_tracebacks(request_data):
    r = copy.deepcopy(request_data)
    r["logs"][0]["text"] = '  File "/app/src/app.py", line 3'
    other = {
        **r["logs"][0],
        "chunk_id": "second",
        "stream": "stdout",
        "text": "SyntaxError: expected ':'",
    }
    r["logs"].append(other)
    found = extract_error_locations(prepare(DiagnosisRequest.model_validate_json(json.dumps(r))))
    assert found == []


def test_repeated_locations_merge_only_same_stream_and_bound_size(request_data):
    text = "src/a.ts:2:3: error TS1005: ';' expected\n" * 20
    found = extract_error_locations(bundle(request_data, text))
    assert len(found) == 1 and len(found[0].evidence_ids) == 8
    text = "\n".join(f"src/a{i}.ts:{i + 1}:3: error TS1005: test" for i in range(30))
    found = extract_error_locations(bundle(request_data, text))
    assert len(found) == 12 and len(location_ranges(found)) == 3
    assert len(json.dumps(location_payload(found), ensure_ascii=False).encode()) <= 3072


@pytest.mark.parametrize(
    "ending",
    [
        "INFO build succeeded",
        "startup complete",
        "healthy",
        "fixture run: this error is expected",
    ],
)
def test_recovery_keeps_observed_location_but_does_not_prefetch(request_data, ending):
    found = extract_error_locations(
        bundle(request_data, "src/a.ts:2:3: error TS1005: test\n" + ending)
    )
    assert found and not location_ranges(found)
    assert location_payload(found)[0]["line"] == 2


def test_distant_lines_do_not_create_unbounded_windows(request_data):
    found = extract_error_locations(
        bundle(request_data, "src/a.ts:3:3: error TS1005: test\nsrc/a.ts:900:4: error TS1005: test")
    )
    r = location_ranges(found)
    assert len(r) == 1 and r[0]["end_line"] == 11
    fitted, limits = fit_location_ranges({"src/a.ts": {"lines": ["x"]}}, r, found)
    assert fitted == [] and limits


def test_eof_diagnostic_reads_real_lines_without_fabricating_source(request_data):
    found = extract_error_locations(
        bundle(request_data, "src/a.js:3\nSyntaxError: Unexpected end of input")
    )
    fitted, limits = fit_location_ranges(
        {"src/a.js": {"lines": ["function f() {", "return 1;"]}},
        location_ranges(found),
        found,
    )
    assert fitted[0]["end_line"] == 2 and "EOF" in limits[0]


def test_lint_path_heading_is_included_in_provenance(request_data):
    found = extract_error_locations(bundle(request_data, "/app/src/a.js\n  3:4  error  semi"))
    assert set(found[0].evidence_ids) == {"EV000001", "EV000002"}


def test_timestamp_and_startup_error_wrapper_preserve_ev(request_data):
    text = (
        "2026-10-04T01:02:03.123Z [startup-error] TypeError: value is undefined\n"
        "    at start (file:///app/src/server.js:42:8)"
    )
    found = extract_error_locations(bundle(request_data, text))
    assert [(x.path, x.line, x.column) for x in found] == [("src/server.js", 42, 8)]
    assert set(found[0].evidence_ids) == {"EV000001", "EV000002"}


@pytest.mark.parametrize(
    "text",
    [
        "x" * 4000 + ".js:2:3: error: bad",
        "x" * 4000,
        "src/a.ts:12:9999999999: error: bad",
    ],
)
def test_long_tokens_cannot_be_reinterpreted_as_shorter_paths(request_data, text):
    assert extract_error_locations(bundle(request_data, text)) == []


def api_payload(request_data, text, path, lines=250):
    request = copy.deepcopy(request_data)
    request["logs"][0]["text"] = text
    return DiagnoseAPIRequest.model_validate_json(
        json.dumps(
            {
                "diagnosis": request,
                "source_snapshot": {"files": [{"path": path, "content": "// context\n" * lines}]},
            }
        )
    )


def located_response(analysis_data, path, line, sc_number):
    value = source_response(analysis_data)
    value["summary"] = "오류 위치와 제공된 코드 문맥을 확인했습니다."
    value["remediation"]["plans"][0]["changes"][0]["target"] = path
    finding = value["source_findings"]["findings"][0]
    finding.update(
        path=path,
        start_line=line,
        end_line=line,
        source_evidence_ids=[f"SC{sc_number:06d}"],
        explanation="제공된 오류 위치의 코드입니다.",
    )
    return value


@pytest.mark.parametrize("mode", ["adaptive", "graph_compact"])
def test_long_file_error_prefetch_one_call_without_docker_or_compact(
    request_data, analysis_data, mode, caplog
):
    class NoCompact:
        async def prepare(self, *args):
            raise AssertionError("syntax errors must not spend a graph/compact model call")

    create, seen, _ = factory([located_response(analysis_data, "src/app.ts", 150, 9)])
    result = asyncio.run(
        diagnose_with_source(
            api_payload(request_data, CASES[0][1], "src/app.ts"),
            create,
            diagnosis_settings=DiagnosisSettings(mode),
            compact_service=NoCompact(),
        )
    )
    DiagnoseAPIResult.model_validate(result)
    assert result["job_status"] == "succeeded"
    assert result["source_analysis"]["findings"][0]["start_line"] == 150
    assert result["source_analysis"]["read_ranges"][0]["start_line"] == 142
    assert len(seen) == 1 and len(seen[0][1]["source_evidence"]) == 17
    assert seen[0][1]["error_locations"][0]["column"] == 7
    assert "Docker" not in seen[0][0]
    assert "세미콜론 생략" in seen[0][0]
    assert "error_locations" not in result
    assert result["remediation_execution"] == "not_executed"


def test_standard_keeps_two_stages_and_resolves_empty_selection(request_data, analysis_data):
    first = selection(analysis_data)
    first["source_request"]["files"] = []
    create, seen, _ = factory([first, located_response(analysis_data, "src/app.ts", 150, 9)])
    result = asyncio.run(
        diagnose_with_source(
            api_payload(request_data, CASES[0][1], "src/app.ts"),
            create,
        )
    )
    assert result["job_status"] == "succeeded"
    assert len(seen) == 2 and "source_evidence" not in seen[0][1]
    assert seen[0][1]["error_locations"][0]["line"] == 150
    assert result["source_analysis"]["read_ranges"][0]["start_line"] == 142


@pytest.mark.parametrize("case", ["absent", "out_of_range", "source_budget", "recovered"])
def test_unavailable_or_inapplicable_locations_keep_log_diagnosis(
    request_data, analysis_data, case
):
    text = CASES[0][1]
    path, lines = "src/app.ts", 250
    if case == "absent":
        path = "other/app.ts"
    elif case == "out_of_range":
        lines = 10
    elif case == "recovered":
        text += "\nINFO build succeeded"
    payload = api_payload(request_data, text, path, lines)
    if case == "source_budget":
        source_lines = ["// context"] * 250
        source_lines[141:158] = ["//" + "x" * 900] * 17
        file = payload.source_snapshot.files[0].model_copy(
            update={"content": "\n".join(source_lines) + "\n"}
        )
        payload = payload.model_copy(
            update={"source_snapshot": payload.source_snapshot.model_copy(update={"files": [file]})}
        )
    create, seen, _ = factory([selection(analysis_data, False)])
    result = asyncio.run(
        diagnose_with_source(payload, create, diagnosis_settings=DiagnosisSettings("graph_compact"))
    )
    assert result["job_status"] == "succeeded" and len(seen) == 1
    assert "source_evidence" not in seen[0][1]


def test_s3_root_scoped_general_location_does_not_read_docker(analysis_data):
    data = envelope()
    data["data"]["source"]["rootDirectory"] = "apps/web"
    data["data"]["logs"][0]["text"] = "src/app.ts(150,7): error TS1005: ';' expected."
    files = [
        ("repo-abcdef1/apps/web/src/app.ts", "// context\n" * 200),
        ("repo-abcdef1/apps/other/src/app.ts", "WRONG_PROJECT"),
        ("repo-abcdef1/apps/web/Dockerfile", "DOCKER_MUST_NOT_BE_READ"),
        ("repo-abcdef1/apps/web/.env", "TOKEN=SECRET_MUST_NOT_BE_READ"),
    ]
    downloads = []

    def transport(request):
        downloads.append(request)
        return httpx.Response(200, content=tarball(files))

    create, seen, _ = factory([located_response(analysis_data, "src/app.ts", 150, 9)])
    response = send(
        create_app(
            create,
            dev=True,
            diagnosis_settings=DiagnosisSettings("graph_compact"),
            archive_transport=httpx.MockTransport(transport),
        ),
        json=data,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    DiagnoseAPIResult.model_validate(result)
    assert len(downloads) == len(seen) == 1
    assert [r["path"] for r in result["source_analysis"]["read_ranges"]] == ["src/app.ts"]
    serialized = json.dumps(seen) + response.text
    for marker in (
        "WRONG_PROJECT",
        "DOCKER_MUST_NOT_BE_READ",
        "SECRET_MUST_NOT_BE_READ",
        "secret-signature",
    ):
        assert marker not in serialized


def test_optional_hints_cannot_overflow_model_budget(request_data):
    from ai_error_check_agent.direct_api import DirectModelProfile

    locations = extract_error_locations(bundle(request_data, CASES[0][1]))
    profile = DirectModelProfile(profile_id="profile-demo-a", model_id="test", max_prompt_bytes=10)
    assert with_error_locations("p", {"logs": []}, {}, locations, profile) == ("p", {"logs": []})
