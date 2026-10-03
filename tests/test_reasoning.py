import asyncio
import copy
import gzip
import json
from pathlib import Path

import pytest
from rdflib import RDF, Graph, Literal, URIRef
from test_source_analysis import api_input, factory, selection, source_response

from ai_error_check_agent.api import create_app
from ai_error_check_agent.knowledge.context import augment, prompt_size
from ai_error_check_agent.knowledge.facts import extract
from ai_error_check_agent.knowledge.reasoner import R, run_reasoning, validate_reasoning
from ai_error_check_agent.knowledge.reasoning import ReasoningService, ReasoningSettings
from ai_error_check_agent.knowledge.shadow import KnowledgeSettings
from ai_error_check_agent.source_analysis import diagnose_with_source
from ai_error_check_agent.source_contracts import DiagnoseAPIRequest, DiagnoseAPIResult


def payload(texts, *, sequences=None, sources=None, code=None):
    rows = []
    for n, text in enumerate(texts):
        row = {
            "id": f"EV{n + 1:06d}",
            "text": text,
            "source_id": sources[n] if sources else "app",
            "stage": "runtime",
            "stream": "stdout",
            "sequence": sequences[n] if sequences else n,
            "event_line": n + 1,
        }
        rows.append(row)
    return {
        "scope": {"attempt_id": "a1"},
        "logs": rows,
        "source_evidence": [
            {"id": f"SC{n + 1:06d}", "path": "src/config.py", "line": n + 1, "text": line}
            for n, line in enumerate((code or "").splitlines())
        ],
    }


def port_payload(**kwargs):
    return payload(
        [
            "listener process_instance=web-1 port=3000",
            "probe process_instance=web-1 port=8080 route=direct result=connection_refused",
            "listener_snapshot process_instance=web-1 only_port=3000 complete=true",
        ],
        sequences=[0, 1, 1],
        **kwargs,
    )


def kinds(report):
    return {c["kind"] for c in report["candidates"]}


def config_payload(code='import os\nurl = os.environ["DATABASE_URl"]'):
    return payload(
        [
            "config_contract process_instance=web-1 role=database key=DATABASE_URL path=src/config.py",
            "config_error process_instance=web-1 role=database key=DATABASE_URl path=src/config.py",
        ],
        code=code,
    )


def test_port_two_rounds_and_provenance():
    report = run_reasoning(port_payload())
    assert kinds(report) == {"observed_port_difference", "port_mismatch_candidate"}
    candidate = next(c for c in report["candidates"] if c["status"] == "candidate")
    assert candidate["evidence_ids"] == ["EV000001", "EV000002", "EV000003"]
    assert report["metrics"]["conforms"]
    assert report["trace"] == run_reasoning(port_payload())["trace"]
    assert all(c["missing_information"] for c in report["candidates"])


@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "attempt",
        "proxy",
        "mapping",
        "multi",
        "stale",
        "missing_sequence",
        "different_source",
    ],
)
def test_port_counterexamples_do_not_promote(change):
    data = port_payload()
    if change == "identity":
        data["logs"][1]["text"] = data["logs"][1]["text"].replace("web-1", "web-2")
    elif change == "attempt":
        data["logs"][2]["text"] += " attempt=older"
    elif change in {"proxy", "mapping"}:
        data["logs"][1]["text"] = data["logs"][1]["text"].replace("direct", change)
    elif change == "multi":
        data["logs"][2]["text"] = (
            "listener_snapshot process_instance=web-1 ports=3000,8080 complete=true"
        )
    elif change == "stale":
        data["logs"][2]["sequence"] = 0
    elif change == "missing_sequence":
        data["logs"][2].pop("sequence")
    else:
        data["logs"][2]["source_id"] = "another-source"
    assert "port_mismatch_candidate" not in kinds(run_reasoning(data))


def test_unknown_process_is_not_source_id():
    data = port_payload()
    for row in data["logs"]:
        row["text"] = row["text"].replace("process_instance=web-1 ", "")
    result = run_reasoning(data)
    assert result["facts"] and not result["candidates"]
    assert "process_identity_missing" in result["limitations"]


def test_recovery_is_scoped_and_later_failure_is_counterevidence():
    texts = [
        f"operation process_instance=web-1 operation=connect target=db result={s}"
        for s in ("failed", "succeeded", "failed")
    ]
    report = run_reasoning(payload(texts))
    assert kinds(report) == {"failure_followed_by_recovery"}
    assert report["candidates"][0]["counter_evidence_ids"] == ["EV000003"]
    for key, value in [
        ("operation=connect", "operation=migrate"),
        ("target=db", "target=cache"),
        ("web-1", "web-2"),
    ]:
        changed = [texts[0], texts[1].replace(key, value)]
        assert not run_reasoning(payload(changed))["candidates"]
    assert not run_reasoning(payload(texts[:2], sequences=[2, 1]))["candidates"]
    assert not run_reasoning(payload(texts[:2], sources=["app", "other"]))["candidates"]


def test_configuration_requires_contract_code_and_runtime_error():
    report = run_reasoning(config_payload())
    assert "configuration_mismatch_candidate" in kinds(report)
    candidate = next(c for c in report["candidates"] if c["status"] == "candidate")
    assert candidate["evidence_ids"] == ["EV000001", "EV000002"]
    assert candidate["source_evidence_ids"] == ["SC000002"]
    data = config_payload()
    data["logs"] = data["logs"][:1]
    assert kinds(run_reasoning(data)) == {"configuration_reference_difference"}


@pytest.mark.parametrize(
    "code",
    [
        'import os\n# os.environ["DATABASE_URl"]',
        'import os\nurl = os.environ["DATABASE_URL"]',
        'import os\nurl = os.environ.get("DATABASE_URl", "default")',
        'import os\nkey = "DATABASE_URl"\nurl = os.environ[key]',
        'import os\nos = {}\nurl = os.environ["DATABASE_URl"]',
        'import os\ndef f(os):\n    return os.environ["DATABASE_URl"]',
    ],
)
def test_code_counterexamples(code):
    assert "configuration_mismatch_candidate" not in kinds(run_reasoning(config_payload(code)))


def test_partial_source_is_not_guessed():
    data = config_payload()
    data["source_evidence"] = data["source_evidence"][1:]
    assert "partial_python_source" in extract(data)["limitations"]
    assert not run_reasoning(data)["candidates"]


def test_shacl_rejects_invalid_port_and_cross_scope_relation():
    graph = Graph()
    node, evidence = URIRef("urn:node"), URIRef("urn:ev")
    for triple in [
        (node, RDF.type, R.Fact),
        (node, R.scope, Literal("scope")),
        (node, R.extractor_version, Literal("v1")),
        (node, R.evidence, evidence),
        (evidence, RDF.type, R.Evidence),
        (node, R.port, Literal(70000)),
    ]:
        graph.add(triple)
    with pytest.raises(ValueError):
        validate_reasoning(graph)
    graph.set((node, R.port, Literal(80)))
    validate_reasoning(graph)
    relation = URIRef("urn:derived")
    for triple in [
        (relation, RDF.type, R.DerivedRelation),
        (relation, R.scope, Literal("other")),
        (relation, R.input, node),
        (relation, R.input, evidence),
        (relation, R.kind, Literal("test")),
        (relation, R.rule, Literal("v1")),
    ]:
        graph.add(triple)
    with pytest.raises(ValueError):
        validate_reasoning(graph)


def test_context_budget_preserves_original_input_and_common_facts():
    report = run_reasoning(port_payload())
    original = {"logs": port_payload()["logs"]}
    frozen = copy.deepcopy(original)
    prompt, data, ctx = augment("base", original, {}, report, 32768)
    assert original == frozen and data["logs"] == original["logs"]
    assert ctx["candidates"] and prompt_size(prompt, data, {}) <= 32768
    _, _, facts = augment("base", original, {}, report, 32768, variant="facts")
    assert ctx["facts"] == facts["facts"] and not facts["candidates"]
    assert augment("base", original, {}, report, 50) == ("base", original, None)


def test_worker_lifecycle_busy_timeout_and_recovery():
    async def check():
        service = ReasoningService(ReasoningSettings(mode="assist"))
        try:
            assert (await service.infer(port_payload()))["status"] == "not_ready"
            assert await service.wait_ready()
            old_pid = service.process.pid
            assert (await service.infer(port_payload()))["status"] == "ok"
            first = asyncio.create_task(service.infer(port_payload()))
            await asyncio.sleep(0)
            assert (await service.infer(port_payload()))["status"] == "busy"
            assert (await first)["status"] == "ok"
            assert service.process.pid == old_pid

            class SlowReader:
                async def readline(self):
                    await asyncio.sleep(30)

            service.process.stdout = SlowReader()
            assert (await service.infer(port_payload()))["status"] == "timed_out"
            assert await service.wait_ready()
            assert service.process.pid != old_pid
            assert (await service.infer(port_payload()))["status"] == "ok"
        finally:
            await service.aclose()
        assert service.process is None

    asyncio.run(check())


def test_off_and_schema_preserved(request_data, analysis_data):
    async def check():
        original = DiagnoseAPIRequest.model_validate_json(json.dumps(api_input(request_data)))
        baseline, seen, _ = factory([selection(analysis_data, False)])
        disabled, seen_disabled, _ = factory([selection(analysis_data, False)])
        result = await diagnose_with_source(original, baseline)
        service = ReasoningService(ReasoningSettings())
        other = await diagnose_with_source(original, disabled, reasoning_service=service)
        assert seen == seen_disabled
        assert result["analysis"] == other["analysis"]
        a = create_app(baseline, dev=True)
        b = create_app(disabled, dev=True, reasoning_settings=ReasoningSettings(mode="assist"))
        assert a.openapi() == b.openapi()
        DiagnoseAPIResult.model_validate(other)

    asyncio.run(check())


def test_two_stages_and_shadow_artifacts(request_data, analysis_data, tmp_path):
    import httpx

    async def check():
        body = api_input(request_data)
        body["diagnosis"]["logs"][0]["text"] = "\n".join(
            r["text"] for r in config_payload()["logs"]
        )
        make, seen, _ = factory([selection(analysis_data), source_response(analysis_data)])
        app = create_app(
            make,
            dev=True,
            reasoning_settings=ReasoningSettings(mode="assist"),
            knowledge_settings=KnowledgeSettings(mode="shadow", output_dir=tmp_path),
        )
        async with app.router.lifespan_context(app):
            assert await app.state.reasoning_service.wait_ready()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.post("/diagnose", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        DiagnoseAPIResult.model_validate(result)
        assert len(seen) == result["execution"]["provider_call_count"] == 2
        assert "reasoning_context" in seen[0][1]
        candidates = seen[1][1]["reasoning_context"]["candidates"]
        assert any(c["kind"] == "configuration_mismatch_candidate" for c in candidates)
        saved = Path(app.state.knowledge_observer.last_report["path"])
        trace = json.loads(gzip.decompress((saved / "reasoning-trace.json.gz").read_bytes()))
        assert [s["stage"] for s in trace] == ["logs", "source"]
        assert all(s["applied_context"] for s in trace)
        assert (saved / "reasoning-facts.json.gz").exists()
        assert (saved / "reasoning-metrics.json").exists()

    asyncio.run(check())


@pytest.mark.parametrize(
    "code",
    [
        'import os\nos.environ["DATABASE_URl"] = os.environ["DATABASE_URL"]\nx = os.environ["DATABASE_URl"]',
        'import os\nos.environ.update({"DATABASE_URl": "safe"})\nx = os.environ["DATABASE_URl"]',
        'import os\nfrom custom import os\nx = os.environ["DATABASE_URl"]',
    ],
)
def test_configuration_aliases_and_mutations_are_not_typo_evidence(code):
    assert not run_reasoning(config_payload(code))["candidates"]


def test_facts_ablation_does_not_execute_rules():
    p = port_payload()
    p["inference"] = False
    report = run_reasoning(p)
    assert len(report["facts"]) == 3
    assert report["trace"] == report["candidates"] == []


def test_worker_cancellation_and_job_recycling():
    async def check():
        service = ReasoningService(ReasoningSettings(mode="assist"))
        try:
            assert await service.wait_ready()
            first_pid = service.process.pid
            assert await service.wait_ready()  # Already warm; no restart.
            assert first_pid == service.process.pid
            service.jobs = 99
            assert (await service.infer(port_payload()))["status"] == "ok"
            assert await service.wait_ready()
            assert first_pid != service.process.pid

            class SlowReader:
                async def readline(self):
                    await asyncio.sleep(30)

            service.process.stdout = SlowReader()
            pending = asyncio.create_task(service.infer(port_payload()))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await service.wait_ready()
            assert (await service.infer(port_payload()))["status"] == "ok"
        finally:
            await service.aclose()
        assert service.process is None

    asyncio.run(check())


def test_size_and_closed_fallback_do_not_call_worker():
    async def check():
        service = ReasoningService(ReasoningSettings(mode="assist"))
        try:
            assert await service.wait_ready()
            assert (await service.infer({"logs": ["x" * 270000]}))["status"] == "too_large"
            assert service.jobs == 0
        finally:
            await service.aclose()
        assert (await service.infer(port_payload()))["status"] == "off"

    asyncio.run(check())


@pytest.mark.parametrize(
    "config",
    [
        {"AGENT_REASONING_MODE": "enforce"},
        {"AGENT_REASONING_TIMEOUT_MS": "nan"},
        {"AGENT_REASONING_TIMEOUT_MS": "-1"},
        {"AGENT_REASONING_TIMEOUT_MS": "100000"},
    ],
)
def test_invalid_settings(config):
    from ai_error_check_agent.errors import DiagnosisError

    with pytest.raises(DiagnosisError):
        ReasoningSettings.from_config(config)


def test_conflicting_complete_snapshots_do_not_promote_port_candidate():
    p = port_payload()
    second = copy.deepcopy(p["logs"][2])
    second.update(id="EV000004", text=second["text"].replace("3000", "8080"), event_line=4)
    p["logs"].append(second)
    assert "port_mismatch_candidate" not in kinds(run_reasoning(p))
