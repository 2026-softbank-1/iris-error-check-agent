# 로컬 실행 상세 (Windows CMD·OpenCode·LLM 설정)

> 2026-10-04 README 재구성 때 기존 README(v0.5.0 시점)에서 원문 그대로 옮겼다. 현재 운영 상태는 [README](../README.md)를 기준으로 한다.

## 진단 API 실행 (v0.5.0)

OpenCode 화면과 서비스 API에서 같은 모델 목록을 선택할 수 있습니다. 기존 GPT 설정은 유지하고, Sakana를 쓰려면 `.env`에 `SAKANA_API_KEY`를 추가합니다.

```bat
run_opencode.cmd
```

OpenCode에서 `iris_diagnosis` 에이전트의 `/models` 메뉴로 GPT 또는 Sakana 모델을 고릅니다. 서비스 연동은 `GET /models`로 목록을 조회하고 `POST /diagnose?model=sakana/fugu`로 선택값을 전달합니다. 선택값이 없으면 기존 `.env` 모델을 사용합니다. 키 없는 모델은 API 목록에서 `available=false`이며 OpenCode 선택 목록에는 나타나지 않습니다. 대화형 화면과 검증된 서비스 진단 절차의 차이, 프론트·백엔드 연결 예시는 [모델 선택 문서](MODEL_SELECTION.md)를 참고하세요.

기존 `.env`의 LLM 설정을 그대로 사용합니다. 의존성을 업데이트한 후 실행합니다.

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
run_api.cmd --dev
```

OpenCode로 동일한 API를 실행하려면 Node.js/npm과 Git이 설치된 상태에서 다음 명령을 사용합니다. OpenCode 1.18.34는 프로젝트의 `.runtime` 폴더에 설치됩니다. 설치는 최초 한 번만 필요합니다.

```bat
install_opencode.cmd
run_api.cmd --runtime opencode --dev
```

API가 전용 OpenCode 프로세스를 시작하고 종료 시 정리합니다. 기존 `.env`의 `LLM_API_KEY`, `LLM_MODEL`을 사용합니다. 자세한 내용은 [OpenCode 실행·검증 문서](OPENCODE_INTEGRATION.md)를 참고하세요.

Docker에서는 API와 OpenCode를 하나의 Linux 이미지로 실행할 수 있습니다. `.env`의 `AGENT_API_KEY`를 별도의 ASCII 32자 이상 키로 설정한 뒤 실행합니다.

```bat
docker build -t iris-error-check-agent:0.5.0 .
docker run --rm --name iris-error-check-agent --env-file .env -e AGENT_RUNTIME=opencode -p 127.0.0.1:8001:8001 --stop-timeout 280 iris-error-check-agent:0.5.0
```

이미지에는 `.env`를 포함하지 않습니다. 자세한 구성과 직접 호출 모드 전환은 [Docker 실행 문서](DOCKER.md)를 참고하세요.

개발 모드는 `127.0.0.1:8001`에서 실행됩니다. [Swagger UI](http://127.0.0.1:8001/docs)에서 요청 규격을 확인할 수 있습니다. 다른 CMD 창에서 합성 로그·소스 샘플을 호출합니다. **실제 LLM 사용량이 발생합니다.**

```bat
curl.exe -X POST http://127.0.0.1:8001/diagnose -H "Content-Type: application/json" --data-binary "@examples/backend-log-only.request.json"
```

서버 모드에서는 `.env`에 별도의 `AGENT_API_KEY`(공백 없는 ASCII 32자 이상)를 설정하고 `run_api.cmd`로 실행합니다. 호출자는 `X-API-Key` 헤더로 인증합니다. `--dev`는 루프백 전용이며, 운영 백엔드용 키를 프론트엔드 번들에 넣지 않습니다.

API 결과는 `diagnosis-result.v3`, 기존 CLI 결과는 `diagnosis-result.v2`입니다. API는 **`success/message/data` 전체 JSON**, `data` 내부 객체, 기존 `diagnosis/source_snapshot` 형식을 모두 받습니다. 새 입력 형식과 S3 규격은 [백엔드 JSON 연동 문서](BACKEND_JSON_V04.md), 기존 파일 직접 전달 방식은 [API·소스 분석 개발 문서](API_SOURCE_ANALYSIS.md)를 참고하세요.

`examples/backend-envelope.request.json`은 합의 중인 JSON 전체 예시입니다. `source.downloadUrl`과 `expiresAt`을 실제 S3 presigned URL과 만료 시각으로 바꾸면 코드 분석이 필요할 때만 다운로드합니다. 소스 없이 테스트할 때는 위의 `backend-log-only.request.json`을 사용합니다. Git 커밋 SHA는 선택 항목이며, `AGENT_SOURCE_ALLOWED_HOSTS`에 허용할 S3 버킷 호스트를 지정할 수 있습니다.

## CMD에서 바로 실행

프로젝트의 `.venv` 설치와 `.env` 입력을 완료했다면 Windows CMD에서 실행합니다.

```bat
cd /d "C:\Users\rlagh\Desktop\소뱅 해커톤\AI_Error_Check_Agent"
run_diagnosis.cmd
```

기본 샘플은 `DATABASE_URL`을 읽지 못한 가상 로그입니다. OpenCode 설치·실행이나 모델 프로필 파일 없이 실제 LLM이 진단합니다. 결과 JSON은 터미널에 출력되며, `job_status`, `analysis.summary`, `analysis.hypotheses`, `analysis.next_checks`, `analysis.remediation`을 확인하면 됩니다.

파일로 저장하거나 다른 로그를 사용하려면 다음처럼 실행합니다. 출력 파일명은 매번 새 이름을 사용하세요.

```bat
run_diagnosis.cmd --request examples\configuration.request.json --output results\diagnosis-001.json
run_diagnosis.cmd --request examples\exit-code-only.request.json
run_diagnosis.cmd --request examples\no-failure-evidence.request.json
```

같은 기능을 Python 모듈로 직접 실행할 수도 있습니다.

```bat
.venv\Scripts\python.exe -m ai_error_check_agent diagnose --request examples\configuration.request.json
```

`.env`에는 다음 네 값을 입력합니다. 기존 키는 그대로 유지하세요.

```dotenv
LLM_API_KEY=발급받은_API_키
LLM_PROVIDER=openai
LLM_MODEL=gpt-6.1-sol
LLM_BASE_URL=https://api.openai.com/v1
```

선택 설정은 `LLM_TIMEOUT_SECONDS=60`, `LLM_MAX_OUTPUT_TOKENS=4096`, `LLM_REASONING_EFFORT=low`입니다. 출력 토큰 한도에는 추론 토큰도 포함됩니다. 현재 디렉터리의 `.env`를 읽으며 다른 파일은 `--env-file`로 지정합니다. 동일 이름의 프로세스 환경변수가 있으면 그 값이 우선합니다. 쉘 명령 실행이나 `${...}` 확장은 하지 않습니다.

OpenAI Fast 모드는 `.env`에 `OPENAI_SERVICE_TIER=fast`를 설정하고 에이전트 서버를 재시작하면 사용합니다. 기존 백엔드 요청·응답 필드와 모델 ID, 추론 노력 설정은 유지합니다. `AGENT_REASONING_MODE=off|assist`와 독립적으로 동작하며, 소스 재분석을 포함한 모든 OpenAI 호출에 적용됩니다. Sakana 모델에는 전달하지 않습니다.

`OPENAI_SERVICE_TIER`를 비워 두거나 생략하면 기존처럼 티어를 요청에 넣지 않아 OpenAI 프로젝트 기본 설정을 따릅니다. `auto`도 프로젝트 설정을 따르고, `default`는 일반 처리를 명시합니다. `fast`와 `priority`는 Fast 모드 요청이며 일반 처리보다 토큰 요금이 높습니다. direct 어댑터는 `service_tier`로 전달하고, 관리형 OpenCode 설정은 SDK 호환성을 위해 `fast`를 동등한 `serviceTier: "priority"`로 전달합니다. 서버가 관리하지 않는 레거시 `--profile` OpenCode 실행은 해당 외부 런타임에서 별도 설정해야 합니다.

실제 처리 티어는 제공자 상황에 따라 요청과 다를 수 있습니다. direct 어댑터는 응답의 티어를 내부 `reported_service_tier`와 INFO 로그 `openai_service_tier`에 기록하며, 응답에 없으면 `unknown`으로 남깁니다. 공개 진단 JSON에는 필드를 추가하지 않습니다. 미지원 모델·계정의 요청 거절은 기존 오류 형식으로 반환하며 자동 재시도하지 않습니다. 설정·HTTP 요청·두 단계 진단·공개 스키마 호환성은 모의 응답으로 검증하며, 계정별 Fast 사용 가능 여부와 실제 속도는 실제 호출로 별도 확인해야 합니다. [OpenAI Fast 모드](https://developers.openai.com/api/docs/guides/fast-mode), [OpenCode 모델 설정](https://opencode.ai/docs/models/), [AI SDK OpenAI 옵션](https://ai-sdk.dev/providers/ai-sdk-providers/openai)을 참고하세요.

API 키는 요청 인증 헤더에만 쓰며 결과·로그에 출력하지 않습니다. 직접 호출은 공식 OpenAI 및 Sakana API 주소를 지원합니다. 호출 실패 시 `error.code`를 확인하세요. `MODEL_AUTH_ERROR`는 키·권한, `MODEL_NOT_FOUND`는 모델 ID·접근 권한, `MODEL_RATE_LIMIT`는 사용량·결제·요청 제한 확인이 필요합니다.

모델 사용량이 발생하며 자동 재시도는 하지 않습니다. [OpenAI 모델 문서](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [구조화 출력 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 기준으로 구현했습니다.

## 설치

Python 3.11 이상을 사용합니다. 아래는 저장소 루트에서 실행하는 PowerShell 예시입니다.

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -e '.[dev]'
```

Linux/macOS에서는 `.venv/bin/python`을 사용합니다. 패키지 이름은 `AI_Error_Check_Agent`, Python import 이름은 `ai_error_check_agent`, 실행 명령은 `ai-error-check`입니다.

검증 때 사용한 의존성 버전은 `requirements-dev.lock`에 기록했습니다. 같은 버전으로 설치하려면 먼저 `python -m pip install -r requirements-dev.lock`, 이어서 `python -m pip install --no-build-isolation --no-deps -e .`를 실행합니다.

## 모델 없이 먼저 확인

```powershell
& ./.venv/Scripts/python.exe -m ai_error_check_agent prepare --request examples/configuration.request.json --output results/prepared.json
& ./.venv/Scripts/python.exe -m ai_error_check_agent validate --request examples/configuration.request.json --analysis examples/configuration.analysis.json
& ./.venv/Scripts/python.exe -m pytest -q
```

`prepare`는 마스킹한 입력과 근거 매핑을 출력합니다. `validate`는 주어진 답변의 구조와 참조를 검사합니다. 두 명령은 모델을 호출하거나 원인을 추론하지 않습니다. 출력 파일이 이미 있으면 덮어쓰지 않으므로 재실행 시 새 파일명을 사용합니다.

`examples/`의 로그와 참조 답변은 모두 **가상 개발 사례**입니다. 참조 답변을 모델 입력에 넣지 않습니다. 실제 장애 성능 평가에 사용할 미사용 사례는 별도로 확보해야 합니다.

## 선택: 기존 OpenCode 실행기 연결

이 절은 기존 CLI에서 직접 관리하는 OpenCode 서버를 `--profile`로 연결할 때만 적용됩니다. 최신 API는 위의 `run_api.cmd --runtime opencode`로 실행하면 별도 프로필 없이 서버를 자동 관리합니다.

1. 팀에서 사용할 OpenCode 버전을 설치·고정하고 공급자 인증을 설정합니다. 모델 API 키는 OpenCode 실행 환경에서 관리합니다.
2. 개인 설정·프로젝트 소스·스킬·플러그인이 없는 전용 실행 환경을 준비합니다. `config/opencode.json`을 적용하고 `127.0.0.1:4096`에서 `opencode serve`를 실행합니다. 상위 디렉터리나 전역 설정이 병합되지 않는지 확인합니다. 단순히 `OPENCODE_CONFIG`만 지정해도 격리가 완료되는 것은 아닙니다.
3. `config/model-profile.example.json`을 별도 파일로 복사하고 실제 `provider_id`, `model_id`, 설치한 `expected_runtime_version`을 입력합니다. 예시 문자열은 사용 가능한 모델 ID가 아닙니다.
4. OpenCode 서버에 HTTP 인증을 설정했다면 `AI_ERROR_OPENCODE_USERNAME`, `AI_ERROR_OPENCODE_PASSWORD`를 프로세스 환경변수로 맞춥니다. 기존 OpenCode 실행 경로에서는 `.env`를 자동 로딩하지 않습니다.
5. 가상 로그부터 실행해 결과와 정리 상태를 확인합니다.

```powershell
& ./.venv/Scripts/python.exe -m ai_error_check_agent diagnose --request examples/configuration.request.json --profile config/model-profile.local.json --output results/diagnosis-001.json
```

모델 프로필의 ID는 요청의 `model_profile_id`와 같아야 합니다. 기본 모델을 추측하거나 다른 제공자로 전환하지 않습니다. 이 단계는 개인 로컬 실험용으로 loopback 런타임만 받습니다. 공유 런타임의 동시 실행은 지원하지 않습니다.

OpenCode는 사전 설정 검사 외에 실제 버전별 동작 확인이 필요합니다. 승인된 설정을 유지하는 별도 프로세스에서만 실행하고, 작업 도중 설정을 변경하지 않습니다. 기본 build 에이전트를 사용하지 않습니다.

참고: [OpenCode Server](https://opencode.ai/docs/server/), [Config](https://opencode.ai/docs/config/), [Permissions](https://opencode.ai/docs/permissions/).

