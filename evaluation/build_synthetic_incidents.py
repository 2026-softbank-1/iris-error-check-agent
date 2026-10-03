"""Build a fixed, labeled deployment benchmark without using a model."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_URL = "https://iris-example.s3.ap-northeast-2.amazonaws.com/snapshots/eval.tar.gz"


def build_cases():
    cases = []

    def add(
        name,
        family,
        logs,
        *,
        status="diagnosed",
        categories=(),
        groups=(),
        anchors=(),
        stage="runtime",
        exit_code=1,
        complete=True,
        files=None,
        fault=None,
        reproduction=None,
    ):
        index = len(cases) + 1
        case_id = f"case-{index:03d}"
        source_files = [{"path": path, "content": text} for path, text in (files or {}).items()]
        digest = hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest()
        payload = {
            "success": True,
            "message": "진단용 데이터 조회 완료",
            "data": {
                "projectId": "eval-project",
                "serviceId": "eval-service",
                "deploymentId": f"eval-deploy-{index:03d}",
                "attemptId": "attempt-1",
                "deploymentStatus": "SUCCEEDED" if status == "no_failure_evidence" else "FAILED",
                "failedStage": stage,
                "exitCode": exit_code,
                "logRange": {
                    "from": "2026-10-03T00:00:00Z",
                    "to": "2026-10-03T00:05:00Z",
                    "isComplete": complete,
                },
                "logs": [
                    {
                        "id": f"event-{n}",
                        "timestamp": f"2026-10-03T00:00:{n:02d}Z",
                        "stage": stage,
                        "sourceId": "deployment-worker",
                        "stream": "combined",
                        "sequence": n,
                        "text": text,
                    }
                    for n, text in enumerate(logs, 1)
                ],
                "source": {
                    "format": "tar.gz",
                    "downloadUrl": ARCHIVE_URL,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "commitSha": digest[:40],
                    "rootDirectory": ".",
                }
                if source_files
                else None,
            },
        }
        cases.append(
            {
                "id": case_id,
                "name": name,
                "family": family,
                "payload": payload,
                "source_files": source_files,
                "gold": {
                    "status": status,
                    "categories": list(categories),
                    "statement_pattern_groups": [list(g) for g in groups],
                    "critical_log_patterns": list(anchors),
                    "source_fault": fault,
                },
                "provenance": reproduction or {"kind": "authored_fault_scenario"},
            }
        )

    # Reproduce known code faults, then remove absolute temporary paths from their logs.
    def python_fault(path, broken, fixed, expected, fault_line, env=None):
        with tempfile.TemporaryDirectory(prefix="iris-synthetic-") as directory:
            base = Path(directory)
            target = base / path
            target.parent.mkdir(parents=True)
            child_env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                **(env or {}),
            }
            target.write_text(broken)
            failed = subprocess.run(
                [sys.executable, "-I", path],
                cwd=base,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            target.write_text(fixed)
            repaired = subprocess.run(
                [sys.executable, "-I", path],
                cwd=base,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert failed.returncode != 0 and expected in failed.stderr
            assert repaired.returncode == 0, repaired.stderr
            log = failed.stderr.replace(str(base.resolve()) + "/", "").replace(str(base) + "/", "")
            return log, {
                "kind": "local_python_fault_injection",
                "broken_exit": failed.returncode,
                "fixed_exit": repaired.returncode,
                "expected_exception": expected,
                "fault_line": fault_line,
                "broken_sha256": hashlib.sha256(broken.encode()).hexdigest(),
                "fixed_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
            }

    path = "src/app.py"
    broken = "def startup()\n    return 'ready'\nstartup()\n"
    log, proof = python_fault(
        path, broken, broken.replace("startup()\n", "startup():\n", 1), "SyntaxError", 1
    )
    add(
        "Python 기동 코드의 콜론 누락",
        "syntax",
        ["INFO validating application", log, "ERROR application validation failed"],
        categories=("build_compile", "other"),
        groups=(("syntax|구문|문법|콜론|colon",),),
        anchors=("SyntaxError",),
        stage="build",
        files={path: broken},
        fault={"path": path, "line": 1},
        reproduction=proof,
    )

    path = "src/config.py"
    broken = (
        "# Deployment provides DATABASE_URL.\nimport os\ndef database_url():\n"
        "    return os.environ['DATABASE_URl']\ndatabase_url()\n"
    )
    fixed = broken.replace("['DATABASE_URl']", "['DATABASE_URL']")
    log, proof = python_fault(
        path, broken, fixed, "KeyError", 4, {"DATABASE_URL": "sqlite:///:memory:"}
    )
    add(
        "환경변수 참조 이름 오타",
        "configuration",
        ["INFO deployment sets DATABASE_URL", log],
        categories=("configuration",),
        groups=(("DATABASE_URl",), ("오타|대소문자|잘못|불일치|typo|case|incorrect|mismatch",)),
        anchors=("KeyError",),
        files={path: broken},
        fault={"path": path, "line": 4},
        reproduction=proof,
    )

    path = "src/server.py"
    broken = "import os\nport = int(os.environ['PORT'])\nprint(port)\n"
    # For this configuration fault the code stays the same; the repaired environment differs.
    with tempfile.TemporaryDirectory(prefix="iris-synthetic-") as directory:
        base = Path(directory)
        target = base / path
        target.parent.mkdir(parents=True)
        target.write_text(broken)
        outputs = []
        for value in ("eighty", "8080"):
            outputs.append(
                subprocess.run(
                    [sys.executable, "-I", path],
                    cwd=base,
                    env={"PORT": value, "PYTHONDONTWRITEBYTECODE": "1"},
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            )
        assert outputs[0].returncode != 0 and "ValueError" in outputs[0].stderr
        assert outputs[1].returncode == 0
        log = outputs[0].stderr.replace(str(base.resolve()) + "/", "").replace(str(base) + "/", "")
        proof = {
            "kind": "local_python_fault_injection",
            "broken_exit": outputs[0].returncode,
            "fixed_exit": outputs[1].returncode,
            "expected_exception": "ValueError",
            "fault_line": 2,
            "fault": "PORT=eighty",
            "repair": "PORT=8080",
        }
    add(
        "PORT의 숫자 변환 실패",
        "configuration",
        ["INFO PORT=eighty", log],
        categories=("configuration", "port_binding", "other"),
        groups=(("PORT|포트",), ("eighty|숫자|정수|integer|변환|invalid literal",)),
        anchors=("ValueError",),
        files={path: broken},
        fault={"path": path, "line": 2},
        reproduction=proof,
    )

    path = "src/main.py"
    broken = "import iris_billing_helpers\nprint('ready')\n"
    log, proof = python_fault(path, broken, "print('ready')\n", "ModuleNotFoundError", 1)
    add(
        "배포 산출물에서 내부 모듈 누락",
        "dependency",
        ["INFO loading application", log],
        categories=("dependency",),
        groups=(("iris_billing_helpers",), ("없|누락|찾|missing|not found|존재|import|포함",)),
        anchors=("ModuleNotFoundError",),
        files={path: broken},
        fault={"path": path, "line": 1},
        reproduction=proof,
    )

    add(
        "필수 설정 누락",
        "configuration",
        [
            "INFO checking required environment",
            "ERROR Missing required configuration: DATABASE_URL",
            "ERROR startup aborted",
        ],
        categories=("configuration",),
        groups=(("DATABASE_URL",), ("누락|미설정|없|missing|required|설정.*않",)),
        anchors=("Missing required configuration",),
    )
    add(
        "리스닝 포트 점유",
        "port",
        [
            "INFO starting HTTP listener",
            "ERROR listen EADDRINUSE: address already in use 0.0.0.0:8080",
            "ERROR application terminated before readiness",
        ],
        categories=("port_binding",),
        groups=(("8080|포트|port",), ("EADDRINUSE|충돌|점유|이미.*사용|already in use",)),
        anchors=("EADDRINUSE",),
    )
    add(
        "플랫폼과 서비스 포트 불일치",
        "port",
        [
            "INFO platform assigned PORT=8080",
            "INFO application listening on 0.0.0.0:3000",
            "ERROR readiness probe to 10.0.0.7:8080 failed: connection refused; deployment failed",
        ],
        categories=("port_binding", "health_check", "configuration"),
        groups=(("3000",), ("8080",), ("불일치|다른|mismatch|포트|port",)),
        anchors=("listening on 0.0.0.0:3000", "probe.*8080"),
    )
    add(
        "루프백 주소에만 바인딩",
        "port",
        [
            "INFO application listening on 127.0.0.1:8080",
            "INFO local request http://127.0.0.1:8080/health returned 200",
            "ERROR platform readiness probe to container IP 10.0.0.7:8080: connection refused",
        ],
        categories=("port_binding", "health_check"),
        groups=(
            ("127\\.0\\.0\\.1|loopback|루프백|localhost",),
            ("바인딩|리스닝|listen|bind|외부|접근",),
        ),
        anchors=("listening on 127.0.0.1", "platform readiness probe"),
    )
    add(
        "볼륨 쓰기 권한 거절",
        "permission",
        [
            "INFO starting as uid=10001",
            "ERROR PermissionError: [Errno 13] Permission denied: '/data/uploads/cache.tmp'",
            "ERROR upload storage initialization failed",
        ],
        categories=("access_permission",),
        groups=(("권한|permission|접근",), ("/data/uploads|cache.tmp|쓰기|write",)),
        anchors=("PermissionError",),
    )
    add(
        "명시적 컨테이너 메모리 초과",
        "resource",
        [
            "WARN memory usage reached container limit",
            "ERROR container termination reason=OOMKilled; memory limit=256MiB; exitCode=137",
            "ERROR deployment failed before readiness",
        ],
        categories=("resource",),
        groups=(("OOM|메모리|memory",),),
        anchors=("OOMKilled",),
        exit_code=137,
    )
    add(
        "릴리스 마이그레이션 대상 테이블 없음",
        "migration",
        [
            "INFO running release migration",
            'ERROR sqlalchemy.exc.ProgrammingError: relation "orders" does not exist',
            "ERROR release migration exited with code 1; runtime stage not started",
        ],
        categories=("release_migration", "external_connection"),
        groups=(("orders",), ("없|존재|누락|does not exist|테이블|table",)),
        anchors=("relation.*orders.*does not exist",),
        stage="release",
    )
    add(
        "기동 실행 파일 없음",
        "command",
        [
            "INFO executing start command: gunicorn app:app",
            "/bin/sh: gunicorn: command not found",
            "ERROR start command exited with code 127",
        ],
        categories=("start_command", "dependency"),
        groups=(("gunicorn",), ("없|찾|설치|missing|not found|누락",)),
        anchors=("gunicorn: command not found",),
        exit_code=127,
    )

    add(
        "정상 배포",
        "healthy",
        [
            "INFO build completed successfully",
            "INFO application ready",
            "INFO health check passed; deployment completed successfully",
        ],
        status="no_failure_evidence",
        exit_code=0,
    )
    add(
        "DB 재시도 뒤 복구",
        "healthy_recovered",
        [
            "WARN database connection refused on attempt 1",
            "INFO attempt 2 connected successfully",
            "INFO application ready; health check passed",
            "INFO deployment completed successfully",
        ],
        status="no_failure_evidence",
        exit_code=0,
    )
    add(
        "테스트의 의도된 오류 출력",
        "healthy_expected_error",
        [
            "INFO running expected-error unit test",
            "ERROR ValueError emitted by negative test fixture",
            "INFO negative test passed: exception was expected",
            "INFO tests: 12 passed, 0 failed",
            "INFO build and deployment completed successfully",
        ],
        status="no_failure_evidence",
        exit_code=0,
    )
    add(
        "첫 빌드 실패 뒤 재시도 성공",
        "healthy_recovered",
        [
            "ERROR attempt 1: dependency download failed",
            "INFO attempt 2: dependencies installed",
            "INFO build succeeded; application ready; health check passed",
            "INFO deployment completed successfully",
        ],
        status="no_failure_evidence",
        exit_code=0,
    )

    add(
        "종료 코드 137만 존재",
        "insufficient",
        ["ERROR process exited with code 137"],
        status="insufficient_evidence",
        exit_code=137,
    )
    add(
        "대상 없는 일반 시간 초과",
        "insufficient",
        ["ERROR operation timed out", "ERROR deployment failed"],
        status="insufficient_evidence",
        exit_code=None,
    )
    add(
        "종료 코드 1만 존재",
        "insufficient",
        ["INFO starting deployment", "ERROR command exited with code 1"],
        status="insufficient_evidence",
    )
    add(
        "잘린 스택 트레이스",
        "insufficient_truncated",
        [
            "Traceback (most recent call last):",
            '  File "src/app.py", line 42, in startup',
            "[log stream truncated]",
        ],
        status="insufficient_evidence",
        complete=False,
    )
    return cases


def main():
    dataset = {
        "version": "synthetic-incidents.v1",
        "dataset_kind": "synthetic_curated",
        "grading_frozen_before_dispatch": True,
        "cases": build_cases(),
    }
    output = ROOT / "evaluation/synthetic_incidents.v1.json"
    output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "cases": len(dataset["cases"]),
                "locally_reproduced": sum(
                    c["provenance"]["kind"] == "local_python_fault_injection"
                    for c in dataset["cases"]
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
