"""Prewarmed inference worker; one request at a time, no network or file writes."""

import json
import sys

from .reasoner import resources, run_reasoning


def main():
    resources()
    run_reasoning({"scope": {}, "logs": []})
    print('{"status":"ready"}', flush=True)
    for _ in range(100):
        raw = sys.stdin.buffer.readline(262146)
        if not raw:
            return 0
        try:
            if len(raw) > 262145 or not raw.endswith(b"\n"):
                raise ValueError("request_limit")
            report = run_reasoning(json.loads(raw))
            encoded = json.dumps(report, ensure_ascii=False)
            if len(encoded.encode()) > 262144:
                raise ValueError("response_limit")
        except Exception:  # noqa: BLE001 - do not expose input in exceptions
            print('{"status":"failed"}', flush=True)
            return 1
        print(encoded, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
