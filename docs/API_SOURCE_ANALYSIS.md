# 진단 API와 소스 분석 개발 문서 — v0.3

`POST /diagnose`는 배포 로그를 먼저 분석하고, 코드 확인이 필요할 때 **백엔드가 보낸 파일**에서 관련 줄 범위를 선택해 추가 분석합니다. 결과에는 원인·해결안과 코드 위치·근거가 포함됩니다. 기존 CLI의 로그 진단도 유지됩니다.

현재 IRIS 백엔드 서버 연결은 보류한 상태입니다. 호출자가 JSON 규격에 맞게 로그와 소스를 전달하면 에이전트는 독립적으로 동작합니다. GitHub 조회, 로컬 저장소 탐색, 수정 코드 실행, 자동 PR·재배포·롤백은 구현 범위에 포함하지 않습니다. MVP의 원클릭 롤백은 별도 배포 기능입니다.

## 실행

저장소 루트의 CMD에서 기존 `.env`를 사용합니다.

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
run_api.cmd --dev
```

- 기본 주소: `http://127.0.0.1:8001`
- Swagger UI: `http://127.0.0.1:8001/docs`
- OpenAPI JSON: `http://127.0.0.1:8001/openapi.json`
- 프로세스 상태: `GET /healthz` (LLM 연결·결제 상태까지 확인하는 엔드포인트는 아님)
- 직접 실행: `.venv\Scripts\python.exe -m ai_error_check_agent.api --dev`
- 종료: 실행 중인 CMD에서 `Ctrl+C`

`--dev`는 루프백 주소에서만 실행됩니다. 서버 모드는 별도의 `AGENT_API_KEY`가 필요합니다. 기존 `.env`를 덮어쓰지 않고 아래 선택 설정을 추가합니다.

```dotenv
AGENT_API_KEY=독립적으로_생성한_공백없는_ASCII_32자이상의_비밀키
AGENT_MODEL_PROFILE_ID=profile-demo-a
AGENT_ALLOWED_ORIGINS=http://localhost:3000
```

키는 다음 명령으로 로컬에서 생성할 수 있습니다. 출력한 값을 비밀값 저장 위치에 보관합니다.

```bat
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
run_api.cmd --host 0.0.0.0 --port 8001
```

외부 서비스에서는 HTTPS 프록시와 사용자·배포별 권한 검사를 담당하는 백엔드에서 호출합니다. 이 API의 공유 서비스 키만으로 tenant/project별 사용자 권한을 검증하지는 않습니다. `tenant_id` 등의 요청값은 호출자가 권한을 확인한 뒤 채워야 합니다. 공유 키나 LLM 키를 공개 프론트엔드 번들에 넣지 않습니다. CORS는 쉼표로 구분한 명시적 origin만 허용하며 기본값은 비어 있습니다.

## 입력 규격

완성된 요청 예시는 [`examples/api-source.request.json`](../examples/api-source.request.json), 로그 전용 예시는 [`examples/api-log-only.request.json`](../examples/api-log-only.request.json)입니다. 모두 합성 데이터입니다.

```json
{
  "diagnosis": {
    "schema_version": "diagnosis-request.v1",
    "tenant_id": "team-iris",
    "project_id": "project-demo",
    "deployment_id": "deploy-001",
    "attempt_id": "attempt-003",
    "trigger": "deployment_failed",
    "context": {"reported_stage": "build", "deployment_status": "failed", "exit_code": 2},
    "logs": [{
      "chunk_id": "chunk-01", "source_id": "build.stderr", "stage": "build",
      "stream": "stderr", "source_line_start": 1, "captured_at": null,
      "is_complete": true,
      "text": "src/server.ts(2,7): error TS2322: Type 'string' is not assignable to type 'number'."
    }],
    "model_profile_id": "profile-demo-a", "model_settings_version": "settings-7",
    "previous_diagnosis_id": null
  },
  "source_snapshot": {
    "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "files": [{
      "path": "src/server.ts",
      "content": "import { createServer } from 'node:http';\nconst port: number = process.env.PORT ?? '3000';\nconst server = createServer((_req, res) => res.end('ok'));\nserver.listen(port, '0.0.0.0');\n"
    }]
  }
}
```

`source_snapshot`은 생략하거나 `null`로 보낼 수 있습니다. 소스를 보내는 경우 실패한 **배포 커밋에 해당하는 전체 파일 내용**을 보내고, 현재 작업 브랜치의 파일과 혼동하지 않습니다. 에이전트가 발췌하므로 줄 번호는 전체 파일의 1-based 위치입니다. 발췌한 내용을 전체 파일처럼 보내면 위치가 어긋납니다.

`commit_sha`는 40자리 16진수이며 백엔드가 주장한 식별자입니다. 에이전트는 내용과 커밋의 일치를 독립적으로 검증하지 않습니다. 응답에 `commit_verification=caller_supplied`를 명시합니다.

| 항목 | 제한 |
| --- | --- |
| HTTP 본문 | 최대 1 MiB, UTF-8 JSON, 중복 키·NaN 거절 |
| 로그 | 기존 1 MiB/10,000줄 계약 + 마스킹 후 근거 JSON 기본 16 KiB 제한 |
| 전달 소스 | 최대 20개, 파일별 UTF-8 64 KiB, 합계 256 KiB |
| 소스 경로 | `/` 구분 상대 경로, 최대 200자, 중복 경로 거절 |
| 비밀·바이너리 파일 | `.env` 계열(`.env.example` 제외), `.git`, SSH/AWS 자격증명, 비밀키 파일, NUL 포함 내용 거절 |
| 선택 범위 | 최대 3개 파일, 파일별 한 범위, 범위별 1~120줄 |
| 모델 전달 코드 | 근거 JSON 합계 8 KiB; 범위를 임의로 잘라서 완전하다고 표시하지 않음 |
| 전체 프롬프트 | 각 호출 기본 32 KiB; 초과하면 명시적 오류 |
| 호출 수 | 로그 1회 + 필요한 경우 코드 1회, 자동 재시도 없음 |
| 시간·동시 요청 | 모델 호출별 `.env` 제한(기본 60초), worker당 동시 2건, 본문 수신 10초 |

경로를 로컬 파일 접근이나 URL 요청에 사용하지 않습니다. 마스킹은 휴리스틱이므로 백엔드에서도 소스의 비밀값을 제거하고 필요한 파일만 보내야 합니다.

## 처리 흐름

1. 인증·본문 크기·입력 규격을 검사하고 로그를 정규화·마스킹합니다.
2. 모델에 로그와 소스 **파일명/줄 수 목록**을 전달합니다. 이 단계에는 소스 내용이 없습니다.
3. 모델이 `source_request`로 조회 필요성·이유·파일·범위·관련 로그 ID를 제시합니다.
4. 필요할 때만 전달된 소스에서 해당 범위를 찾습니다. 전체 파일을 마스킹한 뒤 줄을 선택해 여러 줄에 걸친 비밀값과 원래 줄 번호를 처리합니다.
5. 로그와 선택된 코드의 두 번째 분석으로 최종 진단을 갱신합니다. `EV000001`은 로그, `SC000001`은 코드 근거입니다.
6. 파일·줄 범위·SC/EV/H 참조와 상세 해결안 규격을 검증합니다. 존재하지 않는 위치를 인용하면 코드 분석을 실패 처리하고 유효한 로그 진단을 유지합니다.

수정 예시는 `analysis.remediation.plans[].changes[]`, 적용 후 검증은 `verification[]`에 있습니다. 실제로 읽은 소스 경로나 로그에서 확인한 대상만 `target_known=true`로 인정합니다. 제안은 여전히 검토할 `template`이며 실행·수정·검증 완료를 의미하지 않습니다.

## 응답과 화면 처리

API 응답 버전은 `diagnosis-result.v3`입니다. `analysis`의 내부 구조는 기존 v0.2 분석 스키마를 유지합니다. 기존 CLI 입출력은 바뀌지 않았습니다.

| 필드 | 의미 |
| --- | --- |
| `job_status` | 로그 진단의 성공/실패/시간 초과. 코드 단계 실패 여부는 아래 필드도 확인 |
| `analysis` | 최종 진단. 코드 분석 성공 시 갱신되며 실패 시 로그 진단 유지 |
| `source_analysis.status` | `not_needed`, `unavailable`, `analyzed`, `failed` |
| `source_analysis.reason` | 로그 단계가 판단한 소스 조회 필요성 |
| `source_analysis.requested_files` | 요청한 파일·범위·이유·로그 근거 |
| `source_analysis.read_ranges` | 선택한 파일·줄 범위·마스킹 여부·마스킹 발췌의 SHA-256 |
| `source_analysis.evidence` | `id`, `path`, `line`, `text`로 구성한 마스킹 코드 근거 |
| `source_analysis.findings` | 코드 위치와 `explanation`, `evidence_ids`, `source_evidence_ids`, `hypothesis_ids` |
| `source_analysis.limitations/error` | 누락·범위/예산 초과·마스킹·소스 분석 실패 등의 한계 |
| `execution.stages` | 로그/소스별 시간·모델·사용량·상태·프롬프트/스키마 해시 |
| `execution.tokens` | 단계별 사용량 합계. 하나라도 알 수 없는 항목은 `null` |
| `remediation_execution` | 항상 `not_executed` |

`analyzed`는 선택된 코드를 분석했다는 뜻이며 모든 요청 파일을 확인했다는 뜻은 아닙니다. 일부 파일이 제외됐다면 `read_ranges`와 `limitations`에 표시됩니다. `findings=[]`일 수도 있습니다. 코드 원인이 확인되지 않았을 때 억지로 위치를 만들지 않습니다.

HTTP 200이어도 `source_analysis.status=failed`이면 화면에 **“로그 진단 완료, 코드 분석 실패”**와 한계를 표시합니다. `unavailable`이면 필요한 소스를 추가해 새 요청을 보낼 수 있습니다. 에이전트 자체의 영속 작업·재시도 큐는 없습니다. 동기 응답이므로 프록시·클라이언트 타임아웃은 기본 설정 기준 130초 이상으로 잡습니다. 모델 시간 제한을 바꾸면 호출 2회분과 통신 여유를 반영합니다.

| HTTP | 처리 |
| --- | --- |
| 200 | 로그 진단 성공(코드 분석 상태 별도 확인) |
| 401 | `X-API-Key` 누락/불일치 |
| 408 / 413 / 415 | 본문 수신 시간 / 크기 / Content-Type 오류 |
| 422 | 잘못된 입력·프로필·입력 예산 초과 |
| 429 | 에이전트 동시 처리 한도 초과 |
| 502 / 503 / 504 | 모델 응답 오류 / 접근·연결·결제·한도 오류 / 로그 단계 시간 초과 |

입력·인증 오류는 `{ "error": { "code": "...", "message": "..." } }`, 모델 실행 후 오류는 실행 기록을 포함한 v3 결과를 반환합니다. 오류 메시지에 입력 소스·API 키·공급자 원문을 넣지 않습니다. HTTP 연결이 닫혀도 공급자 측 호출 취소를 보장하지 않으며, 시간 초과 시 `remote_completion_unknown`을 표시하고 자동 재호출하지 않습니다.

## 모듈과 테스트

| 파일 | 책임 |
| --- | --- |
| `src/ai_error_check_agent/api.py` | FastAPI, 인증·본문 제한·CORS·동시 처리·실행 명령 |
| `src/ai_error_check_agent/source_contracts.py` | 요청 소스·조회 범위·코드 근거·응답 계약 |
| `src/ai_error_check_agent/source_analysis.py` | 로그 우선 판단, 인라인 조회, 코드 재분석, 근거 검증 |
| `src/ai_error_check_agent/service.py` | 공통 단일 모델 호출과 실행 기록, 기존 CLI 서비스 |
| `tests/test_api.py`, `tests/test_source_analysis.py` | 외부 LLM 없이 API와 코드 분석 회귀 검사 |
| `evaluation/run_api_source.py` | 실제 LLM을 호출하는 합성 API 시나리오 5개 |

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests evaluation
.venv\Scripts\python.exe evaluation\run_api_source.py --output-dir results\api-source-new-run
```

마지막 명령은 실제 LLM 사용량이 발생합니다. 출력 폴더를 새 이름으로 지정하며 실패 결과도 보존합니다. 최종 실행 결과와 한계는 [v0.3 테스트 보고서](API_SOURCE_TEST_REPORT_2026-10-02.md)에 기록합니다.
