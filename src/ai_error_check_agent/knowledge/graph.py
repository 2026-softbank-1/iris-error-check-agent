"""Deterministic RDF projection of already masked evidence and model assertions."""

import hashlib
from dataclasses import dataclass
from functools import lru_cache

from rdflib import RDF, Graph, Literal, Namespace, URIRef

from ..preprocessing import redact_object

KG = Namespace("urn:iris:knowledge:v1:")
VERSION = "iris-knowledge.v1"


@lru_cache(maxsize=8)
def prepared_query(query, namespaces):
    from rdflib.plugins.sparql import prepareQuery

    return prepareQuery(query, initNs=dict(namespaces))


class ValidationGraph(Graph):
    """Reuse compiled fixed SHACL queries; focus bindings remain request-local."""

    def query(self, query_object, initNs=None, initBindings=None, **kwargs):
        if isinstance(query_object, str):
            namespaces = initNs if initNs is not None else dict(self.namespaces())
            namespace_key = tuple(sorted((str(k), str(v)) for k, v in namespaces.items()))
            query_object = prepared_query(query_object, namespace_key)
        return super().query(query_object, initNs=initNs, initBindings=initBindings, **kwargs)


@dataclass
class Graphs:
    evidence: Graph
    diagnosis: Graph
    root: URIRef


def build_graphs(record, *, max_triples=10_000):
    record = redact_object(record)
    key = hashlib.sha256(record["diagnosis_id"].encode()).hexdigest()
    base = f"urn:iris:diagnosis:{key}:"
    root, attempt = URIRef(base + "diagnosis"), URIRef(base + "attempt")
    evidence = ValidationGraph()
    evidence.bind("kg", KG)

    def node(graph, suffix, kind, *, stage=None):
        ref = URIRef(base + suffix)
        graph.add((ref, RDF.type, kind))
        graph.add((root, KG.validationTarget, ref))
        if kind != KG.Attempt:
            graph.add((ref, KG.inAttempt, attempt))
        if stage:
            graph.add((ref, RDF.type, KG.RelationNode))
            graph.add((ref, KG.inStage, stage))
        return ref

    def values(graph, ref, data, fields):
        for field, predicate in fields.items():
            if data.get(field) is not None:
                graph.add((ref, predicate, Literal(data[field])))

    node(evidence, "diagnosis", KG.Diagnosis)
    node(evidence, "attempt", KG.Attempt)
    values(
        evidence,
        attempt,
        record["scope"],
        {
            "tenant_id": KG.tenantId,
            "project_id": KG.projectId,
            "deployment_id": KG.deploymentId,
            "attempt_id": KG.attemptId,
        },
    )
    context = record.get("backend_context") or {}
    values(evidence, attempt, context, {"service_id": KG.serviceId})
    values(
        evidence,
        attempt,
        record.get("deployment_context") or {},
        {
            "deployment_status": KG.deploymentStatus,
            "reported_stage": KG.failedStage,
            "exit_code": KG.exitCode,
        },
    )
    evidence.add((root, KG.diagnosisId, Literal(record["diagnosis_id"])))

    for item in record.get("evidence", []):
        ref = node(evidence, "log/" + item["id"], KG.LogEvidence)
        evidence.add((ref, RDF.type, KG.Evidence))
        values(
            evidence,
            ref,
            item,
            {
                "id": KG.localId,
                "text": KG.text,
                "source_id": KG.sourceId,
                "log_id": KG.backendLogId,
                "timestamp": KG.timestamp,
                "sequence": KG.sequence,
                "event_line": KG.eventLine,
                "stage": KG.logStage,
                "stream": KG.stream,
            },
        )

    source = record.get("source") or {}
    if source.get("evidence"):
        snapshot = node(evidence, "snapshot", KG.Snapshot)
        values(
            evidence,
            snapshot,
            source,
            {
                "commit_sha": KG.commitSha,
                "archive_sha256": KG.archiveSha256,
            },
        )
        evidence.add(
            (
                snapshot,
                KG.commitVerification,
                Literal(source.get("commit_verification") or "not_supplied"),
            )
        )
        for item in source["evidence"]:
            ref = node(evidence, "code/" + item["id"], KG.CodeEvidence)
            evidence.add((ref, RDF.type, KG.Evidence))
            evidence.add((ref, KG.fromSnapshot, snapshot))
            values(
                evidence,
                ref,
                item,
                {
                    "id": KG.localId,
                    "text": KG.text,
                    "path": KG.filePath,
                    "line": KG.line,
                },
            )

    diagnosis = ValidationGraph()
    diagnosis.bind("kg", KG)
    for triple in evidence:
        diagnosis.add(triple)

    def links(ref, predicate, ids, prefix):
        for local_id in ids:
            # Undefined references stay undefined, so SHACL can report them.
            diagnosis.add((ref, predicate, URIRef(base + prefix + local_id)))

    for item in record.get("stages", []):
        name = item["stage"]
        stage = node(diagnosis, "stage/" + name, KG.Stage)
        diagnosis.add((stage, KG.name, Literal(name)))
        analysis = item.get("analysis") or {}
        prefix = name + "/"
        values(diagnosis, stage, analysis, {"analysis_status": KG.analysisStatus})
        for obs in analysis.get("observations", []):
            ref = node(diagnosis, prefix + obs["id"], KG.Observation, stage=stage)
            values(diagnosis, ref, obs, {"id": KG.localId, "kind": KG.kind, "text": KG.statement})
            links(ref, KG.supports, obs.get("evidence_ids", []), "log/")
        for hypothesis in analysis.get("hypotheses", []):
            ref = node(diagnosis, prefix + hypothesis["id"], KG.Hypothesis, stage=stage)
            values(
                diagnosis,
                ref,
                hypothesis,
                {"id": KG.localId, "category": KG.category, "statement": KG.statement},
            )
            diagnosis.add((ref, KG.confirmation, Literal("unverified")))
            links(ref, KG.supports, hypothesis.get("evidence_ids", []), "log/")
            links(ref, KG.opposes, hypothesis.get("counter_evidence_ids", []), "log/")
            links(ref, KG.observation, hypothesis.get("observation_ids", []), prefix)
        for check in analysis.get("next_checks", []):
            ref = node(diagnosis, prefix + check["id"], KG.NextCheck, stage=stage)
            values(diagnosis, ref, check, {"id": KG.localId, "method": KG.method})
            links(ref, KG.hypothesis, check.get("hypothesis_ids", []), prefix)
        for plan in (analysis.get("remediation") or {}).get("plans", []):
            ref = node(diagnosis, prefix + plan["id"], KG.RemediationPlan, stage=stage)
            values(diagnosis, ref, plan, {"id": KG.localId})
            links(ref, KG.hypothesis, plan.get("hypothesis_ids", []), prefix)
            links(ref, KG.supports, plan.get("evidence_ids", []), "log/")
        if name == "source":
            for number, finding in enumerate(source.get("findings") or []):
                ref = node(diagnosis, prefix + f"finding/{number}", KG.SourceFinding, stage=stage)
                values(
                    diagnosis,
                    ref,
                    finding,
                    {
                        "path": KG.filePath,
                        "start_line": KG.startLine,
                        "end_line": KG.endLine,
                        "explanation": KG.statement,
                    },
                )
                links(ref, KG.hypothesis, finding.get("hypothesis_ids", []), prefix)
                links(ref, KG.logEvidence, finding.get("evidence_ids", []), "log/")
                links(ref, KG.codeEvidence, finding.get("source_evidence_ids", []), "code/")
        if len(diagnosis) > max_triples:
            raise ValueError("knowledge graph triple limit exceeded")
    if len(evidence) > max_triples or len(diagnosis) > max_triples:
        raise ValueError("knowledge graph triple limit exceeded")
    return Graphs(evidence, diagnosis, root)
