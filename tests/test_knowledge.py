import asyncio
import copy
import gzip
import json
from pathlib import Path

import pytest
from rdflib import RDF, Literal, URIRef
from test_backend_api import envelope, tarball
from test_source_analysis import api_input, factory, selection, source_response

from ai_error_check_agent.api import create_app
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.knowledge.graph import KG, build_graphs
from ai_error_check_agent.knowledge.metrics import (
    competency_checks,
    relation_facts,
    score_relations,
)
from ai_error_check_agent.knowledge.record import record_from_result
from ai_error_check_agent.knowledge.shadow import KnowledgeSettings, ShadowObserver
from ai_error_check_agent.knowledge.validation import validate_graph
from ai_error_check_agent.source_contracts import DiagnoseAPIResult


def send(app, **kwargs):
    import httpx

    async def call():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
        ):
            response = await client.post("/diagnose", **kwargs)
        return response

    return asyncio.run(call())


def diagnose(payload, responses, output_dir=None):
    create, seen, runtimes = factory(
        [item if isinstance(item, Exception) else copy.deepcopy(item) for item in responses]
    )
    settings = KnowledgeSettings(mode="shadow", output_dir=output_dir) if output_dir else None
    app = create_app(create, dev=True, knowledge_settings=settings)
    response = send(app, json=payload)
    return response, seen, runtimes, app.state.knowledge_observer


def stable_result(value):
    value = copy.deepcopy(value)
    value.pop("diagnosis_id")
    for execution in [value["execution"], *value["execution"]["stages"]]:
        execution.pop("started_at", None)
        execution.pop("elapsed_ms", None)
    return value


def saved_record(observer):
    directory = Path(observer.last_report["path"])
    record = json.loads(gzip.decompress((directory / "record.json.gz").read_bytes()))
    metadata = json.loads((directory / "metadata.json").read_text())
    return record, metadata, directory


@pytest.mark.parametrize(
    "case", ["logs", "source", "source_failed", "log_failed", "log_timeout", "no_source"]
)
def test_shadow_preserves_entire_response_and_dispatch(request_data, analysis_data, tmp_path, case):
    payload = api_input(request_data)
    responses = [selection(analysis_data, case != "logs")]
    if case == "source":
        responses.append(source_response(analysis_data))
    elif case == "source_failed":
        responses.append(DiagnosisError("MODEL_TIMEOUT", "safe"))
    elif case == "log_failed":
        responses = [DiagnosisError("MODEL_ERROR", "safe")]
    elif case == "log_timeout":
        responses = [DiagnosisError("MODEL_TIMEOUT", "safe")]
    elif case == "no_source":
        payload.pop("source_snapshot")
    baseline, calls, _, _ = diagnose(payload, responses)
    shadow, shadow_calls, runtimes, observer = diagnose(payload, responses, tmp_path / "records")
    assert baseline.status_code == shadow.status_code
    assert stable_result(baseline.json()) == stable_result(shadow.json())
    assert calls == shadow_calls  # prompt, evidence and response schema remain identical
    assert all(runtime.closed for runtime in runtimes)
    DiagnoseAPIResult.model_validate(shadow.json())
    assert observer.stats["recorded"] == 1
    record, metadata, _ = saved_record(observer)
    assert metadata["diagnosis"]["conforms"]
    assert metadata["diagnosis"]["coverage"] == 1
    assert record["diagnosis_id"] == shadow.json()["diagnosis_id"]
    if case == "source":
        assert [stage["stage"] for stage in record["stages"]] == ["logs", "source"]


def test_shadow_backend_source_download_and_secret_boundary(analysis_data, tmp_path):
    import httpx

    downloads = []
    blob = tarball([("src/config.py", 'import os\nurl = os.environ["DATABASE_URl"]\n')])

    def download(request):
        downloads.append(request)
        return httpx.Response(200, content=blob)

    payload = envelope()
    baseline_create, baseline_seen, _ = factory(
        [selection(analysis_data), source_response(analysis_data)]
    )
    baseline = send(
        create_app(baseline_create, dev=True, archive_transport=httpx.MockTransport(download)),
        json=payload,
    )
    assert len(downloads) == 1
    downloads.clear()
    create, seen, _ = factory([selection(analysis_data), source_response(analysis_data)])
    app = create_app(
        create,
        dev=True,
        archive_transport=httpx.MockTransport(download),
        knowledge_settings=KnowledgeSettings(mode="shadow", output_dir=tmp_path),
    )
    response = send(app, json=payload)
    assert response.status_code == 200
    assert len(downloads) == 1 and len(seen) == 2
    assert seen == baseline_seen and stable_result(response.json()) == stable_result(
        baseline.json()
    )
    record, metadata, directory = saved_record(app.state.knowledge_observer)
    assert record["evidence"][0]["log_id"] == "log-001"
    assert metadata["diagnosis"]["conforms"]
    for file in directory.iterdir():
        data = gzip.decompress(file.read_bytes()) if file.suffix == ".gz" else file.read_bytes()
        assert b"secret-signature" not in data and b"downloadUrl" not in data


@pytest.fixture
def valid_graph(request_data, analysis_data):
    response, _, _, _ = diagnose(
        api_input(request_data), [selection(analysis_data), source_response(analysis_data)]
    )
    return build_graphs(record_from_result(response.json()))


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_evidence",
        "foreign_attempt",
        "support_counter",
        "code_range",
        "code_path",
        "confirmed_claim",
        "missing_type",
        "counter_foreign_attempt",
        "check_foreign_stage",
        "missing_code_range",
    ],
)
def test_mutations_are_detected(valid_graph, mutation):
    graph, root = valid_graph.diagnosis, valid_graph.root
    hypothesis = next(graph.subjects(RDF.type, KG.Hypothesis))
    support = next(graph.objects(hypothesis, KG.supports))
    if mutation == "unknown_evidence":
        graph.add((hypothesis, KG.supports, URIRef("urn:missing:EV999999")))
    elif mutation == "foreign_attempt":
        graph.set((support, KG.inAttempt, URIRef("urn:foreign:attempt")))
    elif mutation == "support_counter":
        graph.add((hypothesis, KG.opposes, support))
    elif mutation == "code_range":
        finding = next(graph.subjects(RDF.type, KG.SourceFinding))
        graph.set((finding, KG.endLine, Literal(99)))
    elif mutation == "code_path":
        finding = next(graph.subjects(RDF.type, KG.SourceFinding))
        graph.set((finding, KG.filePath, Literal("unread.py")))
    elif mutation == "confirmed_claim":
        graph.set((hypothesis, KG.confirmation, Literal("confirmed")))
    elif mutation == "counter_foreign_attempt":
        other = URIRef("urn:foreign:log")
        graph.add((other, RDF.type, KG.LogEvidence))
        graph.add((other, KG.inAttempt, URIRef("urn:foreign:attempt")))
        graph.add((hypothesis, KG.opposes, other))
    elif mutation == "check_foreign_stage":
        check = next(graph.subjects(RDF.type, KG.NextCheck))
        graph.set((hypothesis, KG.inStage, URIRef("urn:foreign:stage")))
        graph.add((check, KG.hypothesis, hypothesis))
    elif mutation == "missing_code_range":
        finding = next(graph.subjects(RDF.type, KG.SourceFinding))
        graph.remove((finding, KG.startLine, None))
    else:
        graph.remove((support, RDF.type, None))
    report, _ = validate_graph(graph, root)
    assert not report["conforms"] or report["coverage"] < 1


def test_stage_local_ids_and_request_scope_do_not_collide(request_data, analysis_data, tmp_path):
    response, _, _, observer = diagnose(
        api_input(request_data),
        [selection(analysis_data), source_response(analysis_data)],
        tmp_path,
    )
    record, _, _ = saved_record(observer)
    graph = build_graphs(record).diagnosis
    hypotheses = list(graph.subjects(RDF.type, KG.Hypothesis))
    assert len(hypotheses) == 2
    assert {str(graph.value(h, KG.localId)) for h in hypotheses} == {"H1"}
    other = copy.deepcopy(record)
    other["diagnosis_id"] = response.json()["diagnosis_id"] + "-other"
    assert not set(hypotheses) & set(
        build_graphs(other).diagnosis.subjects(RDF.type, KG.Hypothesis)
    )
    assert all(value is True or value is None for value in competency_checks(graph).values())


def test_relation_metric_uses_reviewed_gold_and_can_detect_extra_relation(valid_graph):
    facts = relation_facts(valid_graph.diagnosis)
    assert ("source:H1", "supports", "log:EV000001") in facts
    metrics = score_relations({("a", "r", "b"), ("a", "r", "wrong")}, {("a", "r", "b")})
    assert metrics["precision"] == 0.5 and metrics["recall"] == 1
    assert metrics["f1"] == pytest.approx(2 / 3)
    assert score_relations(set(), set())["f1"] is None


def test_optional_commit_not_promoted_and_record_masks_again(request_data, analysis_data):
    payload = api_input(request_data)
    payload["source_snapshot"].pop("commit_sha")
    response, _, _, _ = diagnose(
        payload, [selection(analysis_data), source_response(analysis_data)]
    )
    result = response.json()
    result["evidence"][0]["text"] += "\nAPI_KEY=sk-super-secret-1234567890123456"
    record = record_from_result(result)
    assert "sk-super-secret" not in json.dumps(record)
    graph = build_graphs(record).diagnosis
    snapshot = next(graph.subjects(RDF.type, KG.Snapshot))
    assert graph.value(snapshot, KG.commitSha) is None
    assert str(graph.value(snapshot, KG.commitVerification)) == "not_supplied"
    assert validate_graph(graph, build_graphs(record).root)[0]["conforms"]


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "assist"},
        {"timeout_seconds": float("nan")},
        {"max_concurrent": 0},
        {"max_triples": 99},
    ],
)
def test_bounded_settings(settings):
    with pytest.raises(ValueError):
        KnowledgeSettings(**settings)


@pytest.mark.parametrize("failure", ["storage", "timeout", "busy", "observer_exception"])
def test_shadow_failure_does_not_change_api_response(
    request_data, analysis_data, tmp_path, failure
):
    settings = KnowledgeSettings(
        mode="shadow",
        output_dir=tmp_path / "records",
        timeout_seconds=0.05 if failure == "timeout" else 5,
    )
    if failure == "storage":
        settings.output_dir.write_text("not a directory")
    create, _, _ = factory([selection(analysis_data, False)])
    app = create_app(create, dev=True, knowledge_settings=settings)
    observer = app.state.knowledge_observer
    if failure == "busy":
        observer.active = 1
    elif failure == "observer_exception":

        def broken(*_):
            raise RuntimeError("unsafe-detail")

        observer.submit = broken
    response = send(app, json=api_input(request_data))
    assert response.status_code == 200 and response.json()["job_status"] == "succeeded"
    assert "unsafe-detail" not in response.text
    if failure != "observer_exception":
        status = {"storage": "failed", "timeout": "timed_out", "busy": "busy"}[failure]
        assert observer.stats[status] == 1
        assert observer.active == (1 if failure == "busy" else 0)


def test_openapi_is_identical_with_shadow(tmp_path):
    off = create_app(lambda: None, dev=True).openapi()
    shadow = create_app(
        lambda: None,
        dev=True,
        knowledge_settings=KnowledgeSettings(mode="shadow", output_dir=tmp_path),
    ).openapi()
    assert off == shadow


def test_cancelled_observer_kills_worker(monkeypatch, tmp_path):
    killed = []
    waiting = asyncio.Event()

    class Process:
        returncode = None

        def __init__(self):
            self.stdin = self.stdout = self

        def write(self, _):
            pass

        async def drain(self):
            pass

        async def readline(self):
            waiting.set()
            await asyncio.Future()

        def kill(self):
            killed.append(True)
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def spawn(*_, **__):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    observer = ShadowObserver(KnowledgeSettings(mode="shadow", output_dir=tmp_path))

    async def run():
        task = asyncio.create_task(
            observer.observe(
                {
                    "diagnosis_id": "diag-test",
                    "scope": {},
                    "deployment_context": {},
                    "job_status": "failed",
                },
                [],
            )
        )
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert killed == [True] and observer.active == 0


def evaluation_module(monkeypatch, name):
    import importlib

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "evaluation"))
    return importlib.import_module(name)


def test_offline_replay_with_independent_labels_and_no_model(
    request_data, analysis_data, tmp_path, monkeypatch
):
    module = evaluation_module(monkeypatch, "run_knowledge")
    response, _, _, _ = diagnose(api_input(request_data), [selection(analysis_data, False)])
    input_path = tmp_path / "case.json"
    input_path.write_text(response.text, "utf-8")
    # Deliberately incomplete/wrong labels exercise scoring; these are NOT a real gold dataset.
    report = module.evaluate(
        [input_path],
        tmp_path / "evaluation",
        {
            "case.json": {
                "relations": [["logs:H1", "supports", "log:EV999999"]],
                "expected_status": "insufficient_evidence",
            }
        },
    )
    row = report["cases"][0]
    assert report["completed"] == 1 and row["validation"]["conforms"]
    assert row["relation_metrics"]["f1"] == 0 and row["status_correct"] is False
    assert (tmp_path / "evaluation/summary.json").is_file()
    unlabelled = module.evaluate([input_path], tmp_path / "unlabelled")
    assert "relation_metrics" not in unlabelled["cases"][0]


def test_worker_storage_is_masked_and_atomic_under_limits(valid_graph, tmp_path):
    from ai_error_check_agent.knowledge.worker import persist, process_record

    summary = {"timing": {}}
    with pytest.raises(ValueError, match="byte limit"):
        persist(
            {"diagnosis_id": "too-large"},
            valid_graph,
            summary,
            (valid_graph.evidence, valid_graph.diagnosis),
            tmp_path / "limited",
            max_bytes=1,
        )
    assert not list((tmp_path / "limited").iterdir())
    record = {
        "diagnosis_id": "masked-worker",
        "scope": {"tenant_id": "t", "project_id": "p", "deployment_id": "d", "attempt_id": "a"},
        "stages": [],
        "evidence": [{"id": "EV000001", "text": "API_KEY=sk-super-secret-1234567890123456"}],
    }
    output = process_record(record, tmp_path / "records")
    directory = Path(output["path"])
    for path in directory.iterdir():
        data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        assert b"sk-super-secret" not in data
    with pytest.raises(FileExistsError):
        process_record(record, tmp_path / "records")


def test_large_record_skips_worker_and_triple_limit_is_enforced(valid_graph, tmp_path, monkeypatch):
    async def forbidden(*_, **__):
        raise AssertionError("oversized records must not spawn a worker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    observer = ShadowObserver(KnowledgeSettings(mode="shadow", output_dir=tmp_path))
    result = {
        "diagnosis_id": "large",
        "scope": {},
        "deployment_context": {},
        "job_status": "failed",
        "evidence": [{"id": "EV1", "text": "x" * 270_000}],
    }
    assert asyncio.run(observer.observe(result, [])) == "too_large"
    assert observer.active == 0 and observer.stats["too_large"] == 1
    record = {
        "diagnosis_id": "triples",
        "scope": {},
        "evidence": [{"id": f"EV{n}", "text": "log"} for n in range(100)],
    }
    with pytest.raises(ValueError, match="triple limit"):
        build_graphs(record, max_triples=100)


def test_backend_evaluation_shadow_hook_without_paid_calls(
    request_data, analysis_data, tmp_path, monkeypatch
):
    from ai_error_check_agent.direct_api import DirectModelProfile

    module = evaluation_module(monkeypatch, "run_backend_envelope")
    payload = api_input(request_data)
    case = ("config", payload, "diagnosed", "analyzed", "src/config.py")
    # Names must be unique because the evaluator writes one public result per case.
    monkeypatch.setattr(module, "old_cases", lambda: [(f"config-{n}", *case[1:]) for n in range(5)])
    monkeypatch.setattr(
        module,
        "load_direct_settings",
        lambda *_: (
            DirectModelProfile(profile_id="profile-demo-a", model_id="test-model"),
            "fake-key",
        ),
    )
    responses = []
    single_evidence = copy.deepcopy(analysis_data)
    for item in [
        *single_evidence["observations"],
        *single_evidence["hypotheses"],
        *single_evidence["remediation"]["plans"],
    ]:
        item["evidence_ids"] = ["EV000001"]
    for _ in range(4):
        responses.extend([selection(single_evidence), source_response(single_evidence)])
    responses.append(selection(single_evidence))
    create, seen, _ = factory(responses)
    assert (
        asyncio.run(
            module.evaluate(
                tmp_path / "unused.env", tmp_path / "backend", create, knowledge_mode="shadow"
            )
        )
        == 0
    )
    summary = json.loads((tmp_path / "backend/summary.json").read_text())
    assert len(seen) == 9 and summary["completed"] == 5
    assert all(row["knowledge"]["stats"]["recorded"] == 1 for row in summary["cases"])


def test_api_returns_before_blocked_graph_and_shutdown_drains(
    request_data, analysis_data, tmp_path, monkeypatch
):
    import httpx

    resumed = asyncio.Event()
    killed = []

    class Process:
        returncode = None

        def __init__(self):
            self.stdin = self.stdout = self

        def write(self, value):
            self.record = json.loads(value)

        async def drain(self):
            pass

        async def readline(self):
            await resumed.wait()
            return b'{"status":"recorded","conforms":true,"coverage":1}\n'

        def kill(self):
            killed.append(True)
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn(*_, **__):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    create, _, _ = factory([selection(analysis_data, False)])
    app = create_app(
        create, dev=True, knowledge_settings=KnowledgeSettings(mode="shadow", output_dir=tmp_path)
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await asyncio.wait_for(
                    client.post("/diagnose", json=api_input(request_data)), timeout=1
                )
            observer = app.state.knowledge_observer
            assert response.status_code == 200 and observer.stats["recorded"] == 0
            assert observer.active == 1 and observer.stats["submitted"] == 1
            resumed.set()
        assert app.state.knowledge_observer.stats["recorded"] == 1

    asyncio.run(run())
    assert killed == [True] and app.state.knowledge_observer.active == 0


def test_reused_worker_keeps_snapshots_separate_and_recycles(request_data, analysis_data, tmp_path):
    response, _, _, _ = diagnose(api_input(request_data), [selection(analysis_data, False)])

    async def run():
        async with ShadowObserver(
            KnowledgeSettings(mode="shadow", output_dir=tmp_path)
        ) as observer:
            result = response.json()
            assert observer.submit(result, []) == "submitted"
            assert observer.submit(result, []) == "busy"
            original_id = result["diagnosis_id"]
            result["diagnosis_id"] = "changed-after-submission"
            result["evidence"][0]["text"] = "changed-after-submission"
            await observer.drain()
            record, _, _ = saved_record(observer)
            assert record["diagnosis_id"] == original_id
            assert record["evidence"][0]["text"] != "changed-after-submission"
            assert observer.last_report["worker_reused"] is False
            first = observer._workers[0]
            result = response.json()
            result["diagnosis_id"] = "second-job"
            assert await observer.observe(result, []) == "recorded"
            assert observer.last_report["worker_reused"] is True
            assert observer._workers[0].pid == first.pid
            observer._jobs[0] = 100
            result["diagnosis_id"] = "recycled-job"
            assert await observer.observe(result, []) == "recorded"
            assert observer.last_report["worker_reused"] is False and first.returncode is not None
        assert all(worker is None for worker in observer._workers)

    asyncio.run(run())


def test_timeout_discards_worker_and_next_job_recovers(request_data, analysis_data, tmp_path):
    response, _, _, _ = diagnose(api_input(request_data), [selection(analysis_data, False)])

    async def run():
        observer = ShadowObserver(
            KnowledgeSettings(mode="shadow", output_dir=tmp_path, timeout_seconds=0.05)
        )
        async with observer:
            assert await observer.observe(response.json(), []) == "timed_out"
            assert observer.active == 0 and observer._workers == [None]
            observer.settings = KnowledgeSettings(mode="shadow", output_dir=tmp_path)
            assert await observer.observe(response.json(), []) == "recorded"
            assert not observer.last_report["worker_reused"]

    asyncio.run(run())


def test_cached_queries_match_plain_rdflib_validation(valid_graph):
    from rdflib import Graph

    graph = valid_graph.diagnosis
    hypothesis = next(graph.subjects(RDF.type, KG.Hypothesis))
    graph.add((hypothesis, KG.supports, URIRef("urn:unknown:EV")))
    plain = Graph()
    for triple in graph:
        plain.add(triple)
    cached, _ = validate_graph(graph, valid_graph.root)
    uncached, _ = validate_graph(plain, valid_graph.root)
    assert not cached["conforms"] and cached == uncached


def test_cancel_before_background_task_starts_releases_slot(request_data, analysis_data, tmp_path):
    response, _, _, _ = diagnose(api_input(request_data), [selection(analysis_data, False)])

    async def run():
        observer = ShadowObserver(KnowledgeSettings(mode="shadow", output_dir=tmp_path))
        assert observer.submit(response.json(), []) == "submitted"
        next(iter(observer._tasks)).cancel()
        await observer.aclose()
        assert observer.active == 0 and observer._busy == [False]
        assert observer._workers == [None]

    asyncio.run(run())
