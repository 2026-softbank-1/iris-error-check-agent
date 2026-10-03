"""Authored relation challenge sets. Labels are defined here, never copied from predictions."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(split):
    seed = json.loads((ROOT / "evaluation/synthetic_incidents.v1.json").read_text())["cases"][0][
        "payload"
    ]
    cases = []
    variants = range(6) if split == "test" else range(3)
    scenarios = range(10) if split == "test" else (0, 1, 2, 3, 4, 5, 6, 8)
    for variant in variants:
        for scenario in scenarios:
            actor = f"worker-{split}-{variant}"
            listener = (
                "Listening on http://0.0.0.0:3000" if split == "test" else "listener port=3000"
            ) + f" process_instance={actor}"
            probe = (
                "healthcheck" if split == "test" else "probe"
            ) + f" process_instance={actor} port=8080 route=direct result=connection_refused"
            snapshot = f"listener_snapshot process_instance={actor} only_port=3000 complete=true"
            operation = "task" if split == "test" else "operation"
            failed = f"{operation} process_instance={actor} operation=db-connect target=database result=failed"
            recovered = f"{operation} process_instance={actor} operation=db-connect target=database result=succeeded"
            contract = f"config_contract process_instance={actor} role=database key=DATABASE_URL path=src/config.py"
            error = f"config_error process_instance={actor} role=database key=DATABASE_URl path=src/config.py"
            traceback = 'Traceback (most recent call last):\n  File "src/config.py", line 3, in <module>\nKeyError: DATABASE_URl'
            code = 'import os\n# deployment database connection\nurl = os.environ["DATABASE_URl"]\n'
            if split == "test":
                code = 'import os\n# service bootstrap\nconnection = (os.environ[\n    "DATABASE_URl"\n])\n'
            status, group, categories, patterns, anchors = "diagnosed", "fault", [], [], []
            relation_kinds, source_files = [], []
            if scenario in (0, 1):
                logs = [listener, probe + "\n" + snapshot]
                if scenario == 1:
                    logs = [failed, recovered] + logs
                    relation_kinds.append("failure_followed_by_recovery")
                relation_kinds += ["observed_port_difference", "port_mismatch_candidate"]
                categories = ["port_binding", "health_check", "configuration"]
                patterns = [["3000"], ["8080"], ["불일치", "다르", "mismatch", "잘못", "상이"]]
                anchors = ["3000", "8080"]
            elif scenario in (2, 3):
                logs = [contract, error, traceback]
                if scenario == 3:
                    logs = [failed, recovered] + logs
                    relation_kinds.append("failure_followed_by_recovery")
                source_files = [{"path": "src/config.py", "content": code}]
                relation_kinds += [
                    "configuration_reference_difference",
                    "configuration_mismatch_candidate",
                ]
                categories = ["configuration"]
                patterns = [
                    ["DATABASE_URL"],
                    ["DATABASE_URl"],
                    ["불일치", "오타", "다르", "mismatch", "소문자", "대소문자"],
                ]
                anchors = ["config_contract", "config_error"]
            elif scenario == 4:
                status, group = "no_failure_evidence", "counterexample"
                logs = [
                    listener,
                    probe.replace(
                        "route=direct result=connection_refused", "route=mapped result=succeeded"
                    ),
                    "Mapping host 8080 -> container 3000; deployment succeeded; application is ready.",
                ]
                relation_kinds = ["observed_port_difference"]
            elif scenario == 5:
                status, group = "no_failure_evidence", "counterexample"
                code = 'import os\nos.environ["DATABASE_URl"] = os.environ["DATABASE_URL"]\nurl = os.environ["DATABASE_URl"]\n'
                source_files = [{"path": "src/config.py", "content": code}]
                logs = [
                    contract,
                    "Configured compatibility alias DATABASE_URl -> DATABASE_URL; no missing configuration.",
                    "Application ready; deployment and health check succeeded.",
                ]
            elif scenario == 6:
                status, group = "no_failure_evidence", "healthy"
                logs = [failed, recovered, "Application ready; deployment succeeded."]
                relation_kinds = ["failure_followed_by_recovery"]
            elif scenario == 7:
                status, group = "no_failure_evidence", "healthy"
                logs = [
                    listener,
                    probe.replace("8080", "3000").replace("connection_refused", "succeeded"),
                    "All application health checks passed; deployment succeeded.",
                ]
            elif scenario == 8:
                status, group = "insufficient_evidence", "insufficient"
                logs = [
                    "probe port=8080 route=unknown result=connection_refused",
                    "Target process and listener state were not collected. No other diagnostic information.",
                ]
            else:
                status, group = "insufficient_evidence", "insufficient"
                logs = [
                    "Process terminated with exit code 137. No OOM event, memory metric or termination initiator was collected."
                ]
            # Distinct nuisance conditions, not only renamed ports: reorder independent noise,
            # old-attempt observations, unrelated tasks and quoted documentation.
            noise = [
                "debug scheduler tick; nothing failed",
                "listener process_instance=old-instance port=9000 attempt=old-attempt",
                "Documentation example: probe process_instance=example port=8080 result=failed",
                "task process_instance=maintenance operation=cleanup target=cache result=succeeded",
                "Trace sampling dropped verbose informational entries only.",
                "Build completed; build warnings did not block the deployment.",
            ][variant]
            logs = ([noise] + logs) if variant % 2 else (logs + [noise])
            body = copy.deepcopy(seed)
            body["data"].update(
                projectId="relation-eval",
                serviceId="app",
                deploymentId=f"{split}-{len(cases) + 1}",
                attemptId="attempt-1",
                deploymentStatus="SUCCEEDED" if status == "no_failure_evidence" else "FAILED",
                failedStage="runtime",
                exitCode=0 if status == "no_failure_evidence" else 1,
                source=None,
            )
            body["data"]["logs"] = [
                {
                    "id": f"event-{n}",
                    "timestamp": f"2026-10-03T00:00:{n:02d}Z",
                    "stage": "runtime",
                    "sourceId": "app-process",
                    "stream": "combined",
                    "sequence": n,
                    "text": text,
                }
                for n, text in enumerate(logs, 1)
            ]
            if source_files:
                body["data"]["source"] = {
                    "format": "tar.gz",
                    "downloadUrl": "https://iris-example.s3.ap-northeast-2.amazonaws.com/relation.tar.gz",
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "commitSha": "a" * 40,
                    "rootDirectory": ".",
                }
            physical = [line for event in logs for line in event.splitlines()]

            def ids_containing(fragment, physical=physical):
                return [f"EV{n + 1:06d}" for n, line in enumerate(physical) if fragment in line]

            pairs = {
                "observed_port_difference": ids_containing(listener)
                + ids_containing(probe.split(" route=")[0]),
                "port_mismatch_candidate": ids_containing(listener)
                + ids_containing(probe.split(" route=")[0])
                + ids_containing(snapshot),
                "failure_followed_by_recovery": ids_containing(failed) + ids_containing(recovered),
                "configuration_reference_difference": ids_containing(contract),
                "configuration_mismatch_candidate": ids_containing(contract)
                + ids_containing(error),
            }
            code_ids = [f"SC{n:06d}" for n in (range(3, 6) if split == "test" else [3])]
            expected_relations = [
                {
                    "kind": k,
                    "evidence_ids": sorted(set(pairs[k])),
                    "source_evidence_ids": code_ids if k.startswith("configuration_") else [],
                }
                for k in sorted(relation_kinds)
            ]
            cases.append(
                {
                    "id": f"{split}-{len(cases) + 1:03d}",
                    "name": f"{group}-{scenario}-{variant}",
                    "family": group,
                    "payload": body,
                    "source_files": source_files,
                    "gold": {
                        "status": status,
                        "categories": categories,
                        "statement_pattern_groups": patterns,
                        "critical_log_patterns": anchors,
                        "source_fault": None,
                    },
                    "expected_relation_kinds": sorted(relation_kinds),
                    "expected_relations": expected_relations,
                    "provenance": {
                        "kind": "authored_relation_scenario",
                        "scenario": scenario,
                        "nuisance": variant,
                    },
                }
            )
    return {
        "version": "synthetic-incidents.v1",
        "purpose": "relation-reasoning.v1",
        "split": split,
        "limitations": [
            "Authored synthetic cases; not production accuracy.",
            "Development and test share rule families and supported grammar; test is not an independent external benchmark.",
            "Relation labels cover these scoped derived claims and EV/SC anchors, not every ontology edge.",
            "Regex scoring is only a proxy for semantic root-cause accuracy.",
        ],
        "cases": cases,
    }


if __name__ == "__main__":
    for split in ("dev", "test"):
        target = ROOT / "evaluation" / f"relation_reasoning.{split}.v1.json"
        target.write_text(json.dumps(build(split), ensure_ascii=False, indent=2) + "\n")
        print(target.name)
