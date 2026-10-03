"""Bounded, deterministic extraction from masked EV/SC; no execution or model calls."""

import ast
import hashlib
import json
import re
from collections import defaultdict

VERSION = "reasoning-extract.v1"
TOKEN = r"[A-Za-z0-9_.:/-]{1,180}"
FIELD = re.compile(rf"(?<!\S)([a-z_]+)=({TOKEN})(?=\s|$)")
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
PORT = re.compile(r"\d{1,5}\Z")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def extract(payload):
    """Only recognized event grammars produce typed facts. Other evidence remains with the LLM."""
    facts, limitations = [], set()
    scope = digest(payload["scope"])

    def add(kind, evidence, values, span=None):
        fact = {
            "kind": kind,
            "scope": scope,
            "evidence_ids": [evidence["id"]] if evidence["id"].startswith("EV") else [],
            "source_evidence_ids": [evidence["id"]] if evidence["id"].startswith("SC") else [],
            "extractor_version": VERSION,
            "origin": {k: evidence[k] for k in ("path", "line", "event_line") if k in evidence},
            "span": span or [0, len(evidence["text"])],
            **values,
        }
        fact["id"] = "F" + digest(fact)[:24]
        facts.append(fact)
        if len(facts) > 100:
            raise ValueError("fact_limit")

    for row in payload.get("logs", []):
        text = row["text"].strip()
        fields = dict(FIELD.findall(text))
        if len(fields) != len(FIELD.findall(text)):
            limitations.add("ambiguous_fields")
            continue
        if fields.get("attempt") not in {None, str(payload["scope"].get("attempt_id"))}:
            limitations.add("different_attempt")
            continue
        instance = fields.get("process_instance") or fields.get("container_id")
        # PID alone is deliberately not a process generation identifier.
        values = {"instance": instance} if instance else {}
        group = digest([row.get(k) for k in ("source_id", "stage", "stream")])
        values["group"] = group
        if type(row.get("sequence")) is int:
            values["sequence"] = row["sequence"]
            values["event_line"] = row.get("event_line", 1)
        # Ordering is scoped to the same stream. Timestamps from different clocks are not joined.
        event = text.split(" ", 1)[0].lower()
        if event in {"listener", "listening"}:
            port = fields.get("port")
            match = re.match(
                r"(?i)listening (?:on|at) (?:https?://)?(?:[\w.\[\]:-]+:)(\d{1,5})\b", text
            )
            if port is None and match:
                port = match[1]
            if port and PORT.fullmatch(port) and 1 <= int(port) <= 65535:
                add("ListenerObservation", row, {**values, "port": int(port)})
        elif event in {"probe", "healthcheck"}:
            port = fields.get("port") or fields.get("target_port")
            if port and PORT.fullmatch(port) and 1 <= int(port) <= 65535:
                add(
                    "ProbeObservation",
                    row,
                    {
                        **values,
                        "port": int(port),
                        "route": fields.get("route", "unknown"),
                        "outcome": fields.get("result", "unknown"),
                    },
                )
        elif event == "listener_snapshot":
            # A contemporaneous complete listener snapshot is stronger than absence in logs.
            port = fields.get("only_port")
            if (
                fields.get("complete") == "true"
                and port
                and PORT.fullmatch(port)
                and 1 <= int(port) <= 65535
            ):
                add("ListenerSnapshot", row, {**values, "port": int(port)})
        elif event in {"operation", "task"}:
            result = fields.get("result") or fields.get("status")
            if (
                fields.get("operation")
                and fields.get("target")
                and result in {"failed", "failure", "succeeded", "success"}
            ):
                kind = "FailureEvent" if result in {"failed", "failure"} else "RecoveryEvent"
                add(
                    kind,
                    row,
                    {**values, "operation": fields["operation"], "target": fields["target"]},
                )
        elif event in {"config_contract", "config_error"}:
            key, path = fields.get("key"), fields.get("path")
            if (
                key
                and KEY.fullmatch(key)
                and path
                and not path.startswith("/")
                and ".." not in path.split("/")
            ):
                extra = {**values, "key": key, "path": path}
                if event == "config_contract" and fields.get("role"):
                    add("ConfigContract", row, {**extra, "role": fields["role"]})
                elif event == "config_error" and fields.get("role"):
                    add("ConfigError", row, {**extra, "role": fields["role"]})
        if not instance and event in {
            "listener",
            "listening",
            "probe",
            "healthcheck",
            "operation",
            "task",
            "config_contract",
            "config_error",
        }:
            limitations.add("process_identity_missing")

    by_path = defaultdict(list)
    for row in payload.get("source_evidence", []):
        by_path[row["path"]].append(row)
    for path, rows in sorted(by_path.items()):
        if not path.endswith(".py"):
            limitations.add("unsupported_source_language")
            continue
        by_line = {row["line"]: row for row in rows}
        ordered = sorted(by_line)
        if not ordered or ordered != list(range(1, max(ordered) + 1)):
            limitations.add("partial_python_source")
            continue
        code = "\n".join(by_line[n]["text"] for n in ordered)
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError):
            limitations.add("unparseable_python_source")
            continue
        # Support only explicit import os, with no local rebinding or attribute mutation.
        imported = any(
            isinstance(n, ast.Import) and any(a.name == "os" and not a.asname for a in n.names)
            for n in tree.body
        )
        rebound = any(
            (isinstance(n, ast.Name) and n.id == "os" and isinstance(n.ctx, (ast.Store, ast.Del)))
            or (isinstance(n, ast.arg) and n.arg == "os")
            or (
                isinstance(n, ast.Attribute)
                and isinstance(n.ctx, (ast.Store, ast.Del))
                and isinstance(n.value, ast.Name)
                and n.value.id == "os"
            )
            or (
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == "os"
            )
            for n in ast.walk(tree)
        )
        # Direct environment writes, mutating calls, and import aliases may implement
        # legitimate key mappings; skip rather than falsely diagnosing a typo.
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == "os" for a in n.names):
                rebound = True
            if isinstance(n, ast.Import) and any(
                a.asname == "os" and a.name != "os" for a in n.names
            ):
                rebound = True
            if (
                isinstance(n, ast.Subscript)
                and isinstance(n.ctx, (ast.Store, ast.Del))
                and isinstance(n.value, ast.Attribute)
                and isinstance(n.value.value, ast.Name)
                and n.value.value.id == "os"
            ):
                rebound = True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                obj = n.func.value
                if (
                    isinstance(obj, ast.Attribute)
                    and obj.attr == "environ"
                    and isinstance(obj.value, ast.Name)
                    and obj.value.id == "os"
                    and n.func.attr not in {"get", "keys", "items", "values", "copy"}
                ):
                    rebound = True
        if not imported or rebound:
            limitations.add("unsupported_python_binding")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not isinstance(node.ctx, ast.Load):
                continue
            env = node.value
            if not (
                isinstance(env, ast.Attribute)
                and env.attr == "environ"
                and isinstance(env.value, ast.Name)
                and env.value.id == "os"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and KEY.fullmatch(node.slice.value)
            ):
                continue
            selected = [by_line[n] for n in range(node.lineno, node.end_lineno + 1)]
            add("ConfigReference", selected[0], {"key": node.slice.value, "path": path})
            facts[-1]["source_evidence_ids"] = [r["id"] for r in selected]
            facts[-1]["origin"] = {
                "path": path,
                "start_line": node.lineno,
                "end_line": node.end_lineno,
            }
            facts[-1]["id"] = "F" + digest({k: v for k, v in facts[-1].items() if k != "id"})[:24]
    return {
        "schema_version": VERSION,
        "scope": scope,
        "facts": facts,
        "limitations": sorted(limitations),
    }
