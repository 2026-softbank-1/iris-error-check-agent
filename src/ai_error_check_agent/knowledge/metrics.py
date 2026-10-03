"""Deterministic relation and competency-question scoring, not an LLM judge."""

from rdflib import RDF

from .graph import KG

RELATIONS = (
    KG.supports,
    KG.opposes,
    KG.observation,
    KG.hypothesis,
    KG.codeEvidence,
    KG.logEvidence,
)


def relation_facts(graph):
    def identity(node):
        local = graph.value(node, KG.localId)
        if local is not None:
            stage = graph.value(node, KG.inStage)
            name = graph.value(stage, KG.name) if stage else None
            if name is not None:
                return f"{name}:{local}"
            return f"{'code' if (node, RDF.type, KG.CodeEvidence) in graph else 'log'}:{local}"
        if (node, RDF.type, KG.SourceFinding) in graph:
            stage = graph.value(node, KG.inStage)
            return f"{graph.value(stage, KG.name)}:finding:{str(node).rsplit('/', 1)[-1]}"
        return "undefined:" + str(node).rsplit("/", 1)[-1]

    return {
        (identity(subject), str(predicate).removeprefix(str(KG)), identity(obj))
        for predicate in RELATIONS
        for subject, obj in graph.subject_objects(predicate)
    }


def score_relations(predicted, gold):
    predicted, gold = set(predicted), set(gold)
    matched = len(predicted & gold)
    if not predicted and not gold:
        precision = recall = f1 = None
    else:
        precision = matched / len(predicted) if predicted else 0.0
        recall = matched / len(gold) if gold else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched": matched,
        "predicted": len(predicted),
        "gold": len(gold),
    }


def competency_checks(graph):
    """Structural CQs only. Semantic causal support needs separately reviewed labels."""
    hypotheses = list(graph.subjects(RDF.type, KG.Hypothesis))
    findings = list(graph.subjects(RDF.type, KG.SourceFinding))
    return {
        "hypotheses_have_traceable_log_support": all(
            any((e, RDF.type, KG.LogEvidence) in graph for e in graph.objects(h, KG.supports))
            for h in hypotheses
        )
        if hypotheses
        else None,
        "hypotheses_remain_unverified": all(
            str(graph.value(h, KG.confirmation)) == "unverified" for h in hypotheses
        )
        if hypotheses
        else None,
        "findings_have_actual_code_locations": all(
            any((e, RDF.type, KG.CodeEvidence) in graph for e in graph.objects(f, KG.codeEvidence))
            for f in findings
        )
        if findings
        else None,
        "hypotheses_have_one_traceable_stage": all(
            len(set(graph.objects(h, KG.inStage))) == 1
            and str(graph.value(graph.value(h, KG.inStage), KG.name)) in {"logs", "source"}
            for h in hypotheses
        )
        if hypotheses
        else None,
    }
