"""Local two-layer SPARQL deductions, kept separate from model assertions."""

import hashlib
import time
from functools import lru_cache
from importlib.resources import files

from pyshacl import validate
from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.plugins.sparql import prepareQuery

from .facts import VERSION, digest, extract

R = Namespace("urn:iris:reasoning:v1:")
ROUNDS = (
    ("port_difference", "recovery", "config_difference"),
    ("port_candidate", "config_candidate"),
)
SCALAR_FIELDS = (
    "instance",
    "port",
    "group",
    "sequence",
    "event_line",
    "operation",
    "target",
    "outcome",
    "route",
    "path",
    "key",
    "role",
    "extractor_version",
)
CLAIMS = {
    "observed_port_difference": (
        "관찰된 리스너 포트와 probe 포트가 다르다. 실제 장애 원인은 미확정이다.",
        ["직접 연결 여부", "동일 시점의 활성 리스너", "포트 매핑"],
    ),
    "port_mismatch_candidate": (
        "동일 실행의 직접 probe가 거부됐고, 같은 이벤트의 완전한 리스너 스냅샷 포트가 대상 포트와 다르다.",
        ["관찰된 조건과 실제 배포 네트워크의 일치 확인"],
    ),
    "failure_followed_by_recovery": (
        "같은 실행·작업·대상의 실패 뒤 성공이 기록됐다. 이 실패의 복구만 나타내며 서비스 전체 정상이나 현재 상태는 증명하지 않는다.",
        ["이후 재실패와 다른 장애 확인"],
    ),
    "configuration_reference_difference": (
        "제공된 설정 계약 키와 선택된 코드의 직접 환경변수 참조 키가 다르다.",
        ["두 키의 역할 일치", "실행 코드와 스냅샷 일치", "설정 별칭"],
    ),
    "configuration_mismatch_candidate": (
        "설정 계약과 코드 참조 키가 다르며 같은 역할의 실행 오류가 해당 코드 참조 키를 지목한다.",
        ["실행 커밋은 caller_supplied", "실제 설정 계약과 별칭 확인"],
    ),
}


@lru_cache(maxsize=1)
def resources():
    root = files("ai_error_check_agent.knowledge")
    names = [
        "reasoning_shapes.ttl",
        "reasoning_ontology.ttl",
        "facts.py",
        "reasoner.py",
        "context.py",
        "reasoning_worker.py",
    ] + [f"rules/{n}.rq" for group in ROUNDS for n in group]
    content = {name: root.joinpath(name).read_text("utf-8") for name in names}
    return (
        Graph().parse(data=content["reasoning_shapes.ttl"], format="turtle"),
        {name: prepareQuery(content[f"rules/{name}.rq"]) for group in ROUNDS for name in group},
        {name: hashlib.sha256(value.encode()).hexdigest() for name, value in content.items()},
    )


def validate_reasoning(graph):
    ok, _, _ = validate(
        graph,
        shacl_graph=resources()[0],
        inference="none",
        advanced=False,
        js=False,
        do_owl_imports=False,
        max_validation_depth=6,
    )
    if not ok:
        raise ValueError("invalid_reasoning_graph")
    for node in graph.subjects(RDF.type, R.DerivedRelation):
        for ref in graph.objects(node, R.input):
            if not (
                list(graph.objects(ref, RDF.type))
                and graph.value(ref, R.scope) == graph.value(node, R.scope)
            ):
                raise ValueError("invalid_reasoning_reference")


def run_reasoning(payload):
    started = time.perf_counter()
    extracted = extract(payload)
    facts = extracted["facts"]
    graph = Graph()
    known = {r["id"] for r in payload.get("logs", []) + payload.get("source_evidence", [])}
    lookup = {}
    for fact in facts:
        node = URIRef("urn:iris:fact:" + fact["id"])
        lookup[str(node)] = fact
        graph.add((node, RDF.type, R.Fact))
        graph.add((node, RDF.type, R[fact["kind"]]))
        graph.add((node, R.scope, Literal(extracted["scope"])))
        for field in SCALAR_FIELDS:
            if field in fact:
                if field == "instance":
                    instance = URIRef(
                        "urn:iris:process:" + digest([extracted["scope"], fact[field]])
                    )
                    graph.add((instance, RDF.type, R.ProcessInstance))
                    graph.add((instance, R.scope, Literal(extracted["scope"])))
                    graph.add((node, R.instance, instance))
                else:
                    graph.add((node, R[field], Literal(fact[field])))
        for evidence in fact["evidence_ids"] + fact["source_evidence_ids"]:
            if evidence not in known:
                raise ValueError("invalid_evidence_reference")
            ref = URIRef("urn:iris:evidence:" + evidence)
            graph.add((ref, RDF.type, R.Evidence))
            graph.add((node, R.evidence, ref))
    if len(graph) > 2000:
        raise ValueError("fact_graph_limit")
    built = time.perf_counter()
    validate_reasoning(graph)
    validated = time.perf_counter()
    derived = Graph()
    for rule_group in ROUNDS if payload.get("inference", True) else ():
        pending = Graph()
        for name in rule_group:
            output = graph.query(resources()[1][name]).graph
            for triple in output:
                pending.add(triple)
            if len(pending) + len(derived) > 1000:
                raise ValueError("inference_limit")
        for triple in pending:
            graph.add(triple)
            derived.add(triple)
        if len(set(derived.subjects(RDF.type, R.DerivedRelation))) > 100 or len(graph) > 5000:
            raise ValueError("inference_limit")
    inferred = time.perf_counter()
    validate_reasoning(graph)

    def leaves(node, depth=0):
        if depth > 2:
            raise ValueError("inference_depth")
        if str(node) in lookup:
            return [lookup[str(node)]]
        return [f for ref in sorted(graph.objects(node, R.input)) for f in leaves(ref, depth + 1)]

    candidates, trace = [], []
    for node in sorted(derived.subjects(RDF.type, R.DerivedRelation)):
        kind, rule = str(graph.value(node, R.kind)), str(graph.value(node, R.rule))
        linked = {f["id"]: f for f in leaves(node)}
        ev = sorted({i for f in linked.values() for i in f["evidence_ids"]})
        sc = sorted({i for f in linked.values() for i in f["source_evidence_ids"]})
        counter = []
        if kind == "failure_followed_by_recovery":
            recovered = next(f for f in linked.values() if f["kind"] == "RecoveryEvent")
            counter = sorted(
                {
                    i
                    for f in facts
                    if f["kind"] == "FailureEvent"
                    and all(
                        f.get(k) == recovered.get(k)
                        for k in ("instance", "operation", "target", "group")
                    )
                    and "sequence" in f
                    and (f["sequence"], f["event_line"])
                    > (recovered["sequence"], recovered["event_line"])
                    for i in f["evidence_ids"]
                }
            )
        claim, missing = CLAIMS[kind]
        candidates.append(
            {
                "kind": kind,
                "rule_id": rule,
                "evidence_ids": ev,
                "source_evidence_ids": sc,
                "claim": claim,
                "status": "candidate" if kind.endswith("_candidate") else "derived_relation",
                "counter_evidence_ids": counter,
                "missing_information": missing,
            }
        )
        trace.append(
            {
                "id": str(node),
                "rule_id": rule,
                "kind": kind,
                "input_ids": sorted(str(x) for x in graph.objects(node, R.input)),
                "fact_ids": sorted(linked),
                "evidence_ids": ev,
                "source_evidence_ids": sc,
            }
        )
    candidates.sort(key=lambda c: (c["status"] != "candidate", c["kind"], c["evidence_ids"]))
    ended = time.perf_counter()
    return {
        "status": "ok" if facts else "no_match",
        "facts": facts,
        "candidates": candidates,
        "trace": trace,
        "limitations": extracted["limitations"],
        "versions": {"extractor": VERSION, **resources()[2]},
        "input_sha256": digest(payload),
        "metrics": {
            "fact_count": len(facts),
            "relations": len(trace),
            "triples": len(graph),
            "extract_build_ms": round((built - started) * 1000, 3),
            "validate_before_ms": round((validated - built) * 1000, 3),
            "rules_ms": round((inferred - validated) * 1000, 3),
            "total_ms": round((ended - started) * 1000, 3),
            "conforms": True,
        },
    }
