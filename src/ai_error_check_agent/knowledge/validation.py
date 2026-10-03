"""Local, versioned SHACL validation without network imports or OWL expansion."""

import hashlib
from functools import lru_cache
from importlib.resources import files

from pyshacl import validate
from rdflib import RDF, SH, Graph

from .graph import KG, VERSION


@lru_cache(maxsize=1)
def resources():
    package = files("ai_error_check_agent.knowledge")
    shapes = package.joinpath("shapes.ttl").read_text("utf-8")
    ontology = package.joinpath("ontology.ttl").read_text("utf-8")
    return (
        Graph().parse(data=shapes, format="turtle"),
        {
            "version": VERSION,
            "shapes_sha256": hashlib.sha256(shapes.encode()).hexdigest(),
            "ontology_sha256": hashlib.sha256(ontology.encode()).hexdigest(),
        },
    )


def validate_graph(graph, root):
    shapes, versions = resources()
    conforms, report, _ = validate(
        graph,
        shacl_graph=shapes,
        inference="none",
        advanced=False,
        js=False,
        do_owl_imports=False,
        meta_shacl=False,
        max_validation_depth=6,
    )
    if not isinstance(report, Graph):
        raise TypeError("SHACL could not complete validation")
    expected = set(graph.objects(root, KG.validationTarget))
    covered = set()
    for kind in shapes.objects(None, SH.targetClass):
        covered.update(graph.subjects(RDF.type, kind))
    covered &= expected
    violations = []
    for item in report.subjects(RDF.type, SH.ValidationResult):
        violations.append(
            {
                "focus": str(report.value(item, SH.focusNode)),
                "shape": str(report.value(item, SH.sourceShape)),
                "component": str(report.value(item, SH.sourceConstraintComponent)),
                "path": str(report.value(item, SH.resultPath) or ""),
                "message": str(report.value(item, SH.resultMessage) or ""),
            }
        )
    failed = {item["focus"] for item in violations} & {str(n) for n in covered}
    summary = {
        **versions,
        "conforms": bool(conforms),
        "triples": len(graph),
        "expected_nodes": len(expected),
        "covered_nodes": len(covered),
        "coverage": len(covered) / len(expected) if expected else None,
        "failed_nodes": len(failed),
        "node_pass_rate": (len(covered) - len(failed)) / len(covered) if covered else None,
        "violation_count": len(violations),
        "violations": sorted(violations, key=lambda v: (v["focus"], v["component"], v["path"])),
    }
    return summary, report
