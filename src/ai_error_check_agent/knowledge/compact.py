"""Small, evidence-linked file-read graph, SHACL checks and a SPARQL candidate.

The graph proves only agreement between supplied logs/stack/source. It never
proves packaging failure, snapshot identity, or an entire service's state.
"""

import hashlib
import json
import time
from dataclasses import asdict
from functools import lru_cache
from importlib.resources import files

from ..diagnosis_routing import FRAME, MISSING_FILE, candidate_ranges
from .node_source import file_read
from .packaging import prove_copy_omission


@lru_cache(maxsize=1)
def resources():
    from rdflib import Graph, Namespace
    from rdflib.plugins.sparql import prepareQuery

    root = files("ai_error_check_agent.knowledge")
    shapes = root.joinpath("compact_shapes.ttl").read_text()
    rule = root.joinpath("rules/compact_file_read.rq").read_text()
    return (
        Namespace("urn:iris:compact:v1:"),
        Graph().parse(data=shapes, format="turtle"),
        prepareQuery(rule),
        hashlib.sha256(
            (shapes + rule + root.joinpath("rules/compact_docker_copy.rq").read_text()).encode()
        ).hexdigest(),
    )


def validate(graph, shapes):
    from pyshacl import validate as shacl_validate

    ok, _, _ = shacl_validate(
        graph,
        shacl_graph=shapes,
        inference="none",
        advanced=False,
        js=False,
        do_owl_imports=False,
        max_validation_depth=6,
    )
    if not ok:
        raise ValueError("compact_graph_invalid")


@lru_cache(maxsize=1)
def packaging_query():
    from rdflib.plugins.sparql import prepareQuery

    return prepareQuery(
        files("ai_error_check_agent.knowledge").joinpath("rules/compact_docker_copy.rq").read_text()
    )


def build(request, bundle, source_lines, deployment_context=None):
    started = time.perf_counter()
    candidates = candidate_ranges(bundle)
    if not candidates or not source_lines:
        return None
    selected = candidates[0]
    source_path = selected["path"]
    primary_lines = [r for r in source_lines if r["path"] == source_path]
    if not primary_lines or [r["line"] for r in primary_lines] != list(
        range(1, len(primary_lines) + 1)
    ):
        return None
    by_id = {line.id: line for line in bundle.lines}
    failure_id, frame_id = selected["evidence_ids"]
    failure, frame = by_id[failure_id], by_id[frame_id]
    target = MISSING_FILE.search(failure.text)[1]
    match = FRAME.fullmatch(frame.text)
    runtime_path, number = match[1].removeprefix("file://"), int(match[2])
    read = file_read(primary_lines, runtime_path, number, target)
    if read is None:
        return None

    from rdflib import RDF, Graph, Literal, URIRef

    c, shapes, query, resource_hash = resources()
    scope = hashlib.sha256(
        json.dumps(
            {
                "request": [
                    getattr(request, k)
                    for k in ("tenant_id", "project_id", "deployment_id", "attempt_id")
                ],
                "producer": [failure.source_id, failure.stage, failure.stream],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    graph = Graph()
    base = f"urn:iris:compact:{scope}:"
    failure_node, frame_node, read_node = (
        URIRef(base + name) for name in ("failure", "frame", "read")
    )
    for node, kind, refs in (
        (failure_node, c.ReadFailure, [failure_id]),
        (frame_node, c.StackFrame, [frame_id]),
        (read_node, c.SourceRead, read["source_ids"]),
    ):
        graph.add((node, RDF.type, c.Fact))
        graph.add((node, RDF.type, kind))
        graph.add((node, c.scope, Literal(scope)))
        graph.add((node, c.target, Literal(target)))
        for ref in refs:
            evidence = URIRef(base + ref)
            graph.add((evidence, RDF.type, c.Evidence))
            graph.add((evidence, c.id, Literal(ref)))
            graph.add((node, c.evidence, evidence))
    graph.add((failure_node, c.code, Literal("ENOENT")))
    for node in (frame_node, read_node):
        graph.add((node, c.path, Literal(source_path)))
        graph.add((node, c.line, Literal(number)))
    validate(graph, shapes)
    for triple in graph.query(query).graph:
        graph.add(triple)
    validate(graph, shapes)
    derived = list(graph.subjects(RDF.type, c.Candidate))
    if len(derived) != 1:
        return None
    candidate = derived[0]
    inputs = set(graph.objects(candidate, c.input))
    if inputs != {failure_node, frame_node, read_node} or any(
        graph.value(node, c.scope) != Literal(scope) for node in inputs
    ):
        return None
    # Resolve all model-facing anchors FROM the validated graph, not a guessed ID.
    ids = sorted(
        {str(graph.value(ref, c.id)) for node in inputs for ref in graph.objects(node, c.evidence)}
    )
    candidate_data = {
        "id": "C1",
        "kind": "observed_file_read_failure",
        "target": str(graph.value(candidate, c.target)),
        "code_path": source_path,
        "code_line": number,
        "rule": "observed-file-read.v1",
        "evidence_ids": [i for i in ids if i.startswith("EV")],
        "source_evidence_ids": [i for i in ids if i.startswith("SC")],
        "claim": "로그의 ENOENT 대상과 스택 위치의 소스 읽기 경로가 일치한다. 배포상 원인은 미확정이다.",
    }
    packaging = prove_copy_omission(
        source_lines, deployment_context, source_path, runtime_path, target
    )
    if packaging:
        nodes = []
        for name, kind, predicate, refs in (
            ("entry", c.RepositoryEntry, c.regular, []),
            ("copy", c.CopyCoverage, c.omitted, packaging["source_evidence_ids"]),
            ("ignore", c.IgnorePolicy, c.allows, packaging["ignore_evidence_ids"]),
        ):
            node = URIRef(base + name)
            nodes.append(node)
            for p, v in (
                (RDF.type, c.ArchiveFact),
                (RDF.type, kind),
                (c.scope, Literal(scope)),
                (c.target, Literal(target)),
                (c.repoPath, Literal(packaging["repo_path"])),
                (c.archiveHash, Literal(packaging["archive_sha256"])),
                (predicate, Literal(True)),
            ):
                graph.add((node, p, v))
            for ref in refs:
                evidence = URIRef(base + ref)
                graph.add((evidence, RDF.type, c.Evidence))
                graph.add((evidence, c.id, Literal(ref)))
                graph.add((node, c.evidence, evidence))
        validate(graph, shapes)
        for triple in graph.query(packaging_query()).graph:
            graph.add(triple)
        validate(graph, shapes)
        derived_packaging = list(graph.subjects(RDF.type, c.PackagingCandidate))
        if len(derived_packaging) != 1 or set(graph.objects(derived_packaging[0], c.input)) != {
            candidate,
            *nodes,
        }:
            return None
    # Keep EVERY distinct text and all occurrence IDs/timing. Only byte-identical
    # log text is grouped; no unrecognized or counter evidence is pruned.
    grouped = {}
    for line in bundle.lines:
        item = grouped.setdefault(line.text, {"text": line.text, "occurrences": []})
        meta = asdict(line)
        item["occurrences"].append(
            {
                k: meta[k]
                for k in ("id", "timestamp", "sequence", "event_line", "chunk_id", "chunk_line")
                if k in meta
            }
        )
    packet = {
        "candidate": candidate_data,
        "context": bundle.context,
        "producer": {
            "source_id": failure.source_id,
            "stage": failure.stage,
            "stream": failure.stream,
        },
        "logs": list(grouped.values()),
        "code": {
            "path": source_path,
            "lines": [[r["id"], r["line"], r["text"]] for r in primary_lines],
        },
        "limitations": list(bundle.limitations)
        + [
            "소스 스냅샷과 실제 실행 커밋의 일치를 독립 검증하지 않았다.",
            "제공된 소스 범위만 확인했다. 실제 빌드 문맥·마운트·파일 공급·영속성 계약은 미확인이다.",
        ],
    }
    additional_paths = list(
        dict.fromkeys(r["path"] for r in source_lines if r["path"] != source_path)
    )
    if additional_paths:
        packet["deployment_code"] = [
            {
                "path": path,
                "lines": [
                    [r["id"], r["line"], r["text"]] for r in source_lines if r["path"] == path
                ],
            }
            for path in additional_paths
        ]
    if packaging:
        packet["packaging_candidate"] = packaging
    elif additional_paths:
        # Broader context exists but the restricted rules cannot justify a fixed
        # packaging plan. Let the full source stage reason about those files.
        return None
    ttl = graph.serialize(format="turtle")
    return {
        "packet": packet,
        "report": {
            "stage": "source",
            "variant": "graph_compact",
            "status": "ok",
            "rule": "observed-file-read.v1",
            "resources_sha256": resource_hash,
            "graph_ttl": ttl,
            "candidate": candidate_data,
            "packaging_candidate": packaging,
            "metrics": {
                "conforms": True,
                "triples": len(graph),
                "fact_count": 6 if packaging else 3,
                "candidate_count": 2 if packaging else 1,
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
                "original_log_lines": len(bundle.lines),
                "distinct_log_lines": len(grouped),
                "retained_log_ids": sum(len(x["occurrences"]) for x in grouped.values()),
            },
        },
    }
