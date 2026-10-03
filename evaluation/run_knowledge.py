"""Replay existing v3 results into graphs; no network, model invocation, or API changes."""

import argparse
import json
from pathlib import Path

from ai_error_check_agent.knowledge.graph import build_graphs
from ai_error_check_agent.knowledge.metrics import (
    competency_checks,
    relation_facts,
    score_relations,
)
from ai_error_check_agent.knowledge.record import record_from_result
from ai_error_check_agent.knowledge.worker import process_record
from ai_error_check_agent.source_contracts import DiagnoseAPIResult


def evaluate(inputs, output_dir, gold=None):
    parsed = []
    for path in inputs:
        if path.stat().st_size > 1_048_576:
            raise ValueError("result input too large")
        value = json.loads(path.read_text("utf-8"))
        value = value.get("result", value)
        DiagnoseAPIResult.model_validate_json(json.dumps(value))
        parsed.append((path, value))
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = []
    for path, result in parsed:
        record = record_from_result(result)
        response = process_record(record, output_dir / "knowledge", mode="offline")
        metadata = json.loads((Path(response["path"]) / "metadata.json").read_text("utf-8"))
        graphs = build_graphs(record)
        row = {
            "input": path.name,
            "diagnosis_id": result["diagnosis_id"],
            "validation": metadata["diagnosis"],
            "timing": metadata["timing"],
            "worker_peak_rss_bytes": metadata["worker_peak_rss_bytes"],
            "competency_checks": competency_checks(graphs.diagnosis),
            "relations": sorted(relation_facts(graphs.diagnosis)),
        }
        labels = (gold or {}).get(path.name, {})
        if "relations" in labels:
            # Exact F1 requires complete reviewed labels within this projection's relation scope.
            row["relation_metrics"] = score_relations(
                relation_facts(graphs.diagnosis), map(tuple, labels["relations"])
            )
        if "expected_status" in labels:
            row["status_correct"] = (result.get("analysis") or {}).get("analysis_status") == labels[
                "expected_status"
            ]
        rows.append(row)
    summary = {
        "planned": len(parsed),
        "completed": len(rows),
        "cases": rows,
        "scope": "Offline graph replay, no LLM calls; not diagnostic accuracy evidence",
        "limitations": [
            "Public responses contain final analysis only; prior stage hypotheses cannot be reconstructed.",
            "Relations not labeled as a complete gold set are not assigned F1.",
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    args = parser.parse_args()
    evaluate(
        args.input, args.output_dir, json.loads(args.gold.read_text("utf-8")) if args.gold else None
    )
