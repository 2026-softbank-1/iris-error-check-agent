"""Summarize paired outcomes and inference coverage without changing frozen grading."""

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path


def summarize_run(root):
    summary = json.loads((root / "live-summary.json").read_text())
    cases = json.loads((root / "dataset-frozen.json").read_text())["cases"]
    labels = {c["id"]: c for c in cases}
    rows = json.loads((root / "execution-rows.json").read_text())
    report = {"arms": {}, "paired": {}, "failed_grades": []}
    for arm, items in rows.items():
        modes = Counter()
        applied = 0
        generated = 0
        stored = 0
        hashes = set()
        for meta in (root / arm / "knowledge").glob("*/reasoning-trace.json.gz"):
            stored += 1
            stages = json.loads(gzip.decompress(meta.read_bytes()))
            for stage in stages:
                modes[stage["status"]] += 1
                applied += bool(stage.get("applied_context"))
                generated += bool((stage.get("applied_context") or {}).get("candidates"))
                if stage.get("versions"):
                    hashes.add(json.dumps(stage["versions"], sort_keys=True))
        submissions = sum(r["execution"].get("message_submissions", 0) for r in items)
        report["arms"][arm] = {
            "completed": len(items),
            "metrics": summary[arm]["metrics"],
            "execution_errors": summary[arm]["execution_errors"],
            "submissions": submissions,
            "provider_calls_confirmed": summary[arm]["provider_calls"],
            "provider_count_complete": summary[arm]["provider_call_count_complete"],
            "tokens": summary[arm]["tokens"],
            "latency_ms": summary[arm]["latency_ms"],
            "inference_statuses": dict(modes),
            "context_stages": applied,
            "candidate_stages": generated,
            "saved_reasoning_cases": stored,
            "distinct_implementation_versions": len(hashes),
        }
        for item in items:
            if item["grade"].get("root_cause_correct") is False:
                report["failed_grades"].append(
                    {
                        "arm": arm,
                        "case": item["case_id"],
                        "checks": item["grade"].get("root_checks"),
                        "statement": item["grade"]["primary_statement"],
                        "execution_ok": item["grade"]["execution_ok"],
                    }
                )
    for left, right in [("baseline", "rules"), ("facts", "rules")]:
        by_left = {r["case_id"]: r for r in rows[left]}
        by_right = {r["case_id"]: r for r in rows[right]}
        paired = Counter()
        for case_id in sorted(by_left.keys() & by_right.keys()):
            if labels[case_id]["gold"]["status"] != "diagnosed":
                continue
            a = by_left[case_id]["grade"]["root_cause_correct"]
            b = by_right[case_id]["grade"]["root_cause_correct"]
            paired[
                "both_correct"
                if a and b
                else "left_only"
                if a
                else "right_only"
                if b
                else "both_incorrect"
            ] += 1
        report["paired"][left + "_vs_" + right] = dict(paired)
    # Keep the automatic grade and uncertain semantic interpretation distinct.
    report["limitations"] = [
        "Root-cause scores use frozen regex/category/evidence checks; equivalent wording can fail.",
        "Provider/adapter errors stay in denominators and are not retried.",
        "One draw per case and arm on authored, related templates; no production accuracy claim.",
        "Blinded human semantic review has not been completed.",
    ]
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("directory", type=Path)
    a = p.parse_args()
    report = summarize_run(a.directory)
    (a.directory / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                k: {
                    "completed": v["completed"],
                    "errors": v["execution_errors"],
                    "root": v["metrics"]["root_cause_accuracy"],
                    "inference_statuses": v["inference_statuses"],
                    "context_stages": v["context_stages"],
                }
                for k, v in report["arms"].items()
            },
            ensure_ascii=False,
        )
    )
