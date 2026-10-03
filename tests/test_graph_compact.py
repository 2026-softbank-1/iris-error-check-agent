"""Graph evidence, conservative fallback and response assembly, with mock models."""

import asyncio
import copy
import json
import threading

import pytest
from test_diagnosis_routing import CODE, LOG, payload, response
from test_source_analysis import factory

from ai_error_check_agent.api import create_app
from ai_error_check_agent.diagnosis_routing import DiagnosisSettings
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.graph_compact import CompactService
from ai_error_check_agent.knowledge.compact import build, resources, validate
from ai_error_check_agent.knowledge.node_source import file_read
from ai_error_check_agent.preprocessing import prepare
from ai_error_check_agent.source_analysis import diagnose_with_source, select_source, source_index
from ai_error_check_agent.source_contracts import DiagnoseAPIResult

MODE = DiagnosisSettings("graph_compact")
ACCEPT = {
    "decision": "accept",
    "candidate_id": "C1",
    "plan": "provide_verified_file",
    "summary": "필수 파일을 읽는 중 ENOENT가 발생했습니다.",
    "uncertainty": "실제 파일 공급 단계와 마운트 상태는 확인되지 않았습니다.",
    "counter_evidence_ids": [],
}


def evidence(code):
    return [
        {"id": f"SC{i:06d}", "path": "src/tasks.js", "line": i, "text": line}
        for i, line in enumerate(code.splitlines(), 1)
    ]


def test_graph_and_sparql_link_log_stack_source_and_pass_shacl(request_data):
    p = payload(request_data)
    result = build(p.diagnosis, prepare(p.diagnosis), evidence(CODE))
    report = result["report"]
    assert report["metrics"]["conforms"]
    assert report["metrics"]["fact_count"] == 3 and report["metrics"]["candidate_count"] == 1
    assert report["candidate"]["evidence_ids"] == ["EV000001", "EV000003"]
    assert report["candidate"]["source_evidence_ids"] == ["SC000002"]
    assert report["candidate"]["target"] == "/app/data/tasks.json"


def test_relative_url_resolves_only_against_observed_module_path():
    code = "import { readFileSync } from 'node:fs';\nconst file = new URL('../data/tasks.json', import.meta.url);\nconst tasks = JSON.parse(readFileSync(file, 'utf8'));\n"
    result = file_read(evidence(code), "/app/src/tasks.js", 3, "/app/data/tasks.json")
    assert result["source_ids"] == ["SC000002", "SC000003"]
    assert file_read(evidence(code), "/different/src/tasks.js", 3, "/app/data/tasks.json") is None


@pytest.mark.parametrize(
    "code",
    [
        "const fake = \"readFileSync('/app/data/tasks.json')\";\n",
        "// import { readFileSync } from 'node:fs';\n// readFileSync('/app/data/tasks.json');\n",
        CODE.replace("const tasks", "const readFileSync"),
        CODE + "function readFileSync() {}\n",
        CODE.replace("{ readFileSync }", "{ readFileSync as read }").replace(
            "readFileSync('/", "read('/"
        ),
        CODE.replace("/app/data/tasks.json", "/app/data/other.json"),
        CODE.replace("const tasks =", "try { const tasks =") + "} catch (e) {}",
        CODE.replace("'/app/data/tasks.json'", "process.env.DATA_FILE"),
        CODE.replace("'utf8'", "options"),
        CODE + "globalThis.URL = something;",
        CODE.replace("const tasks", "const regex = /readFileSync/; const tasks"),
    ],
)
def test_unsupported_bindings_paths_and_quoted_code_are_not_facts(code):
    assert file_read(evidence(code), "/app/src/tasks.js", 2, "/app/data/tasks.json") is None


def test_shacl_rejects_dangling_evidence_and_rule_rejects_cross_scope(request_data):
    from rdflib import RDF, Graph, Literal

    p = payload(request_data)
    result = build(p.diagnosis, prepare(p.diagnosis), evidence(CODE))
    c, shapes, query, _ = resources()
    graph = Graph().parse(data=result["report"]["graph_ttl"], format="turtle")
    read = next(graph.subjects(RDF.type, c.SourceRead))
    ref = next(graph.objects(read, c.evidence))
    graph.remove((ref, RDF.type, c.Evidence))
    with pytest.raises(ValueError, match="compact_graph_invalid"):
        validate(graph, shapes)
    graph.add((ref, RDF.type, c.Evidence))
    graph.set((read, c.scope, Literal("other-tenant")))
    assert len(graph.query(query).graph) == 0


def test_all_distinct_text_and_occurrence_ids_survive_compaction(request_data):
    p = payload(request_data, log=LOG + LOG + "deployment note: preserve all records")
    bundle = prepare(p.diagnosis)
    g = build(p.diagnosis, bundle, evidence(CODE))
    rows = g["packet"]["logs"]
    assert {r["text"] for r in rows} == {line.text for line in bundle.lines}
    assert {o["id"] for r in rows for o in r["occurrences"]} == bundle.evidence_ids
    assert len(rows) < len(bundle.lines)


def test_one_compact_call_returns_unchanged_full_contract_and_provenance(request_data):
    create, seen, runtimes = factory([ACCEPT])
    result = asyncio.run(
        diagnose_with_source(payload(request_data), create, diagnosis_settings=MODE)
    )
    DiagnoseAPIResult.model_validate(result)
    assert result["job_status"] == "succeeded", result["error"]
    assert len(seen) == 1 and runtimes[0].closed
    assert "candidate_id" in seen[0][2]["properties"]
    assert result["execution"]["provider_call_count"] == 1
    assert result["source_analysis"]["findings"][0]["source_evidence_ids"] == ["SC000002"]
    assert (
        result["analysis"]["remediation"]["plans"][0]["changes"][0]["target"]
        == "/app/data/tasks.json"
    )
    assert result["remediation_execution"] == "not_executed"
    assert result["analysis"]["remediation"]["reason"] == ACCEPT["uncertainty"]
    assert "provide_verified_file" not in json.dumps(result["analysis"])


@pytest.mark.parametrize("case", ["defer", "wrong_candidate", "language", "bad_schema", "counter"])
def test_compact_rejection_runs_only_one_full_source_fallback(request_data, analysis_data, case):
    first = copy.deepcopy(ACCEPT)
    if case == "defer":
        first.update(decision="defer", plan="defer")
    elif case == "wrong_candidate":
        first["candidate_id"] = "C999"
    elif case == "language":
        first["summary"] = "file missing"
    elif case == "bad_schema":
        first["invented_field"] = True
    else:
        first["counter_evidence_ids"] = ["EV000004"]
    create, seen, runtimes = factory([first, response(analysis_data)])
    result = asyncio.run(
        diagnose_with_source(payload(request_data), create, diagnosis_settings=MODE)
    )
    assert result["job_status"] == "succeeded"
    assert len(seen) == result["execution"]["provider_call_count"] == 2
    assert "source_findings" in seen[1][2]["properties"]
    assert "source_request" not in seen[1][2]["properties"]
    assert all(r.closed for r in runtimes)
    assert len(result["execution"]["stages"]) == 2
    assert result["execution"]["tokens"]["output"] == 100


@pytest.mark.parametrize(
    "code", ["MODEL_TIMEOUT", "MODEL_AUTH_ERROR", "MODEL_RATE_LIMIT", "MODEL_CONNECTION_ERROR"]
)
def test_provider_errors_are_not_retried(request_data, code):
    create, seen, runtimes = factory([DiagnosisError(code, "safe")])
    result = asyncio.run(
        diagnose_with_source(payload(request_data), create, diagnosis_settings=MODE)
    )
    assert result["error"]["code"] == code and len(seen) == 1 and runtimes[0].closed


def test_unrecognized_source_falls_back_before_llm(request_data, analysis_data):
    create, seen, _ = factory([response(analysis_data)])
    p = payload(request_data, code=CODE.replace("'/app/data/tasks.json'", "process.env.DATA_FILE"))
    result = asyncio.run(diagnose_with_source(p, create, diagnosis_settings=MODE))
    assert result["job_status"] == "succeeded"
    assert "source_findings" in seen[0][2]["properties"]


def test_timeout_does_not_queue_another_graph_worker(monkeypatch, request_data):
    from ai_error_check_agent.knowledge import compact

    entered, release = threading.Event(), threading.Event()

    def slow(*_):
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(compact, "build", slow)
    p = payload(request_data)

    async def run():
        service = CompactService(timeout_seconds=0.01)
        try:
            _, first = await service.prepare(p.diagnosis, prepare(p.diagnosis), evidence(CODE))
            assert entered.is_set() and first["status"] == "timed_out"
            _, second = await service.prepare(p.diagnosis, prepare(p.diagnosis), evidence(CODE))
            assert second["status"] == "busy"
        finally:
            release.set()
            await service._task

    asyncio.run(run())


def test_public_openapi_is_identical_and_config_is_internal():
    assert (
        create_app(lambda: None, dev=True).openapi()
        == create_app(
            lambda: None,
            dev=True,
            diagnosis_settings=MODE,
        ).openapi()
    )
    assert DiagnosisSettings.from_config({"AGENT_DIAGNOSIS_MODE": "graph_compact"}) == MODE


def test_template_passes_existing_graph_validation(request_data):
    from ai_error_check_agent.knowledge.graph import build_graphs
    from ai_error_check_agent.knowledge.record import record_from_result
    from ai_error_check_agent.knowledge.validation import validate_graph

    create, _, _ = factory([ACCEPT])
    result = asyncio.run(
        diagnose_with_source(payload(request_data), create, diagnosis_settings=MODE)
    )
    graphs = build_graphs(record_from_result(result))
    assert validate_graph(graphs.diagnosis, graphs.root)[0]["conforms"]


def test_masked_secrets_never_reappear_in_packet(request_data):
    p = payload(
        request_data, code=CODE + '\nconst API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456";'
    )
    idx = source_index(p.source_snapshot)
    selected, _, _ = select_source(idx, [{"path": "src/tasks.js", "start_line": 1, "end_line": 4}])
    g = build(p.diagnosis, prepare(p.diagnosis), selected)
    assert g is not None
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(g)


def test_reviewed_copy_template_refuses_existing_files_and_links(tmp_path):
    import shlex
    import shutil
    import subprocess

    from ai_error_check_agent.graph_compact import template

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the deterministic remediation template test")
    snippet = template()["remediation"]["plans"][0]["changes"][0]["snippet"]
    args = shlex.split(snippet)
    script = args[args.index("-e") + 1]
    source = tmp_path / "verified.json"
    target = tmp_path / "data" / "tasks.json"
    source.write_text('[{"id":"preserve-source"}]')

    def run():
        # Run only the reviewed server template with isolated test paths,
        # never a generated model snippet or observed production path.
        return subprocess.run(
            [node, "--input-type=module", "-e", script, str(source), str(target)],
            capture_output=True,
            check=False,
        )

    assert run().returncode == 0 and target.read_bytes() == source.read_bytes()
    target.write_text('[{"id":"existing-production-data"}]')
    assert run().returncode != 0
    assert target.read_text() == '[{"id":"existing-production-data"}]'
    target.unlink()
    protected = tmp_path / "protected.json"
    protected.write_text("must remain unchanged")
    target.symlink_to(protected)
    assert run().returncode != 0 and protected.read_text() == "must remain unchanged"
