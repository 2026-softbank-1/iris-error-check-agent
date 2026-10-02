# AI_Error_Check_Agent

IRIS 배포 로그를 받아 **관찰 사실 → 근거 있는 원인 후보 → 다음 확인 → 상세 해결안 → 한계**를 반환하는 오류 진단 프로젝트입니다.

현재 산출물은 **진단 코어, 로컬 CLI, `POST /diagnose` API**입니다. `.env`를 읽어 OpenAI Responses API를 직접 호출합니다. API는 먼저 로그를 분석하고, 필요할 때 **백엔드가 요청에 포함한 소스 파일의 관련 범위**를 선택해 재분석합니다. 백엔드 서비스와의 실제 연결, 사용자별 권한 확인, 비동기 작업 DB·Worker, 대시보드, EKS 배치는 후속 구현 대상입니다.

## 진단 API 실행 (v0.3)

기존 `.env`의 LLM 설정을 그대로 사용합니다. 의존성을 업데이트한 후 실행합니다.

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
run_api.cmd --dev
```

개발 모드는 `127.0.0.1:8001`에서 실행됩니다. [Swagger UI](http://127.0.0.1:8001/docs)에서 요청 규격을 확인할 수 있습니다. 다른 CMD 창에서 합성 로그·소스 샘플을 호출합니다. **실제 LLM 사용량이 발생합니다.**

```bat
curl.exe -X POST http://127.0.0.1:8001/diagnose -H "Content-Type: application/json" --data-binary "@examples/api-source.request.json"
```

서버 모드에서는 `.env`에 별도의 `AGENT_API_KEY`(공백 없는 ASCII 32자 이상)를 설정하고 `run_api.cmd`로 실행합니다. 호출자는 `X-API-Key` 헤더로 인증합니다. `--dev`는 루프백 전용이며, 운영 백엔드용 키를 프론트엔드 번들에 넣지 않습니다.

API 결과는 `diagnosis-result.v3`, 기존 CLI 결과는 `diagnosis-result.v2`입니다. API 요청은 기존 입력을 `diagnosis`에 넣고 선택적으로 `source_snapshot`을 추가합니다. [API·소스 분석 개발 문서](docs/API_SOURCE_ANALYSIS.md)에 전체 흐름, 제한, 오류 처리, 백엔드 연결 지점을 정리했습니다.

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

API 키는 요청 인증 헤더에만 쓰며 결과·로그에 출력하지 않습니다. 현재 직접 호출은 공식 OpenAI 주소를 지원합니다. 호출 실패 시 `error.code`를 확인하세요. `MODEL_AUTH_ERROR`는 키·권한, `MODEL_NOT_FOUND`는 모델 ID·접근 권한, `MODEL_RATE_LIMIT`는 사용량·결제·요청 제한 확인이 필요합니다.

모델 사용량이 발생하며 자동 재시도는 하지 않습니다. [OpenAI 모델 문서](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [구조화 출력 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 기준으로 구현했습니다.

## 구현 범위

- 엄격한 입력 규격과 빈 로그·청크 중복·크기 상한 검사
- 비밀값 마스킹, 물리적 줄 위치·출처·근거 ID 유지
- 설계안 v0.1 부록 A/B를 옮긴 프롬프트와 JSON Schema
- `.env` 로딩과 OpenAI Responses API 직접 호출, 도구 미제공, 응답 저장 비활성화
- 진단 가능·정보 부족·실패 근거 없음의 세 상태 및 참조 관계 검증
- 원인·로그 근거에 연결된 수정 코드·설정·명령 예시와 적용 조건, 검증 절차·기대 결과, 롤백·주의점
- 수정 예시의 자리표시자·근거·대상 검사와 비밀값이 포함된 예시 거절
- API 인증·CORS 허용 목록·요청 크기·동시 처리 제한과 Swagger UI
- 백엔드 제공 소스의 조건부 선택, 파일·줄 번호·로그/코드 근거 연결, 소스 실패 시 로그 진단 유지
- OpenCode 전용 에이전트·버전·권한 사전 확인, 요청별 세션·모델 지정
- 진단 시간 초과 시 중단 요청, 세션 삭제와 정리 실패 기록
- 실제 모델 메타데이터·사용량·프롬프트 해시·입력 스냅샷 해시 기록
- 외부 모델을 호출하지 않는 HTTP 계약 테스트와 개발용 가상 사례

가상 입력으로 연결을 확인하는 것과 실제 장애 정확도를 평가하는 것은 별개입니다. 자동 테스트는 HTTP 계약·근거 검증·오류 처리를 확인하며, OpenCode 실제 연동과 실제 장애 성능 평가는 별도로 수행해야 합니다.

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

이 절은 명시적으로 `--profile`을 지정할 때만 적용됩니다. 기본 CMD 실행에는 필요하지 않습니다.

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

## 개발 사례 반복 평가

실제 모델 연결을 확인한 뒤 가상 개발 사례 3개를 각각 3회 실행할 수 있습니다. 아래 명령은 모델을 호출하므로 사용량이 발생합니다.

```powershell
& ./.venv/Scripts/python.exe -m ai_error_check_agent.evaluate --manifest evaluation/development.json --repeats 3 --output results/evaluation-001.json
```

평가 결과는 상태 일치율·실행 오류 수·응답 시간과 각 실행 원본 결과를 포함합니다. 정답 상태는 모델 입력과 분리됩니다. 가상 개발 사례 점수를 실제 장애 정확도로 사용하지 않습니다. 런타임 정리 상태가 불명확하면 나머지 실행을 중단하고 계획 횟수와 완료 횟수를 따로 기록합니다.

오답, 실행 오류, 런타임 정리 중단이 있으면 평가 명령은 종료 코드 1을 반환합니다.

오류 유형과 경계 조건 14개를 추가로 평가하려면 아래 명령을 실행합니다. 실제 API를 14회 호출하므로 사용량이 발생합니다. 결과 디렉터리는 새 이름으로 지정하세요.

```bat
.venv\Scripts\python.exe evaluation\run_comprehensive.py --output-dir results\extended-evaluation-001
```

확장 평가에는 의존성·컴파일·시작 명령·포트·외부 연결·권한·메모리·헬스 체크·마이그레이션 오류, 후속 회복, 누락 로그, 로그 안의 악성 지시, 가상 비밀값 마스킹이 포함됩니다. 상태·주요 원인 분류·근거 ID·원래 배포 상태 보존을 검사하고 사례마다 결과를 저장합니다. 원인에 대한 설명과 다음 확인 방법의 의미적 적합성은 별도로 검토해야 합니다.

상세 해결안 추가 전인 v0.1의 2026-10-02 실행 결과와 제한은 [AI 테스트 보고서](docs/AI_TEST_REPORT_2026-10-02.md)에 기록했습니다. v0.2의 기능과 검증 결과는 [상세 해결안 안내](docs/REMEDIATION_V2.md)를 참고하세요.

최종 전체 회귀 평가에는 기본 사례·확장 사례·종료 코드 137 단독·예상된 테스트 오류를 포함한 19개 시나리오가 있습니다. 회복 사례를 3회 반복하므로 총 21회 호출합니다.

```bat
.venv\Scripts\python.exe evaluation\run_comprehensive.py --manifest evaluation\full-regression.json --output-dir results\full-regression-001
```

## 결과와 상태

`analysis`는 모델의 진단 내용이며 나머지는 실행 코드가 채웁니다.

CLI 출력은 `diagnosis-result.v2`입니다. CLI 입력은 기존 `diagnosis-request.v1`을 유지합니다. API는 이를 감싼 요청과 `diagnosis-result.v3` 응답을 사용합니다. `analysis.remediation`이 필수이므로 백엔드의 결과 DTO와 화면에서 해당 필드를 처리해야 합니다. v0.1의 분석 JSON은 현재 `validate` 명령을 통과하지 않습니다.

| 필드 | 의미 |
| --- | --- |
| `job_status` | `succeeded`, `failed`, `timed_out` |
| `analysis.analysis_status` | `diagnosed`, `insufficient_evidence`, `no_failure_evidence` |
| `analysis.remediation` | 해결안 상태, 수정 예시, 적용 조건, 검증·롤백·주의점 |
| `remediation_execution` | 항상 `not_executed`: 제안한 코드·명령을 실행하지 않음 |
| `evidence` | 서버가 만든 ID에 해당하는 마스킹된 로그·원본 위치 |
| `deployment_context` | 원래 배포 상태의 스냅샷. 진단 결과로 변경하지 않음 |
| `execution` | 모델·시간·사용량·버전·정리 상태 |
| `input_limitations` | 누락·마스킹 등 전처리기가 확인한 제약 |

정보 부족과 실패 근거 없음도 정상적인 진단 결과면 `job_status=succeeded`입니다. 모델 오류·무효 응답에는 가짜 진단을 채우지 않습니다. 근거 ID 검증만으로 주장과 로그의 의미적 일치가 보장되지는 않습니다.

## 상세 해결안 확인

기존 `run_diagnosis.cmd` 명령을 그대로 실행하면 `analysis.remediation`도 반환됩니다. 추가 설정은 필요하지 않습니다.

- `plans[].apply_when`: 수정 전에 확인할 적용 조건
- `plans[].changes[]`: `code`, `configuration`, `command` 중 필요한 수정 예시, 수정 대상과 자리표시자 설명
- `plans[].verification[]`: 수정 후 순서대로 확인할 방법과 `expected_result` 성공 기준
- `plans[].rollback`, `plans[].risks`: 되돌리는 절차와 변경 영향

예시는 실제 원본 코드를 열어 만든 패치가 아니므로 `snippet_kind=template`입니다. `{{DATABASE_URL}}` 같은 자리표시자를 실제 환경에 맞게 채우고 적용 조건을 확인합니다. 로그에서 확인한 대상은 `target_known=true`, 확인하지 못한 대상은 `false`입니다. 이는 대상 문자열이 로그에 있다는 뜻이며 해당 파일이나 설정이 올바르다는 보장은 아닙니다.

원인을 제시할 수 있으면 `status=proposed`로 상세 해결안을 제공합니다. 원인 근거가 부족하면 `needs_more_evidence`, 관련 실패 근거가 없으면 `not_needed`이며 두 경우 모두 `plans=[]`입니다. 코드 오류에는 코드 예시, 설정 오류에는 설정 예시처럼 상황에 필요한 종류를 제공합니다.

상세 해결안 전용 합성 사례 9개로 실제 API를 평가하려면 다음을 실행합니다. 호출 사용량이 발생하며 출력 디렉터리는 새 이름을 사용합니다.

```bat
.venv\Scripts\python.exe evaluation\run_comprehensive.py --manifest evaluation\remediation.json --output-dir results\remediation-evaluation-001
```

직접 API 호출의 시간 초과는 HTTP 연결을 종료하지만 제공자 측 생성 취소를 확인하지 못할 수 있습니다. 이 경우 `abort_confirmed=false`, `cleanup_status=remote_completion_unknown`을 기록하고 평가 반복을 중단합니다. OpenCode 경로에서 `runtime_reusable=false` 또는 정리 실패가 발생하면 전용 프로세스와 잔여 세션을 확인해야 합니다.

## 초기 버전의 제한과 설계안 대비 차이

- 모든 로그를 보존해 제공하며, 모델 입력 예산을 초과하면 거절합니다. 긴 로그 선택·부분 발췌는 아직 구현하지 않았습니다.
- 1 MiB·10,000줄·20청크의 수신 상한과 별도로, 기본 로그 JSON 예산은 16 KiB, 전체 프롬프트 예산은 32 KiB입니다. 이는 **바이트 한도**이며 8,000토큰과 같다는 뜻이 아닙니다. 실제 모델 토크나이저·문맥·출력 예산 연결은 후속 과제입니다.
- 직접 호출은 HTTP 요청을 한 번만 보내며 자동 재시도하지 않습니다. OpenCode 내부 재시도·제목 생성 등에 의한 호출 횟수는 아직 검증하지 않아 해당 경로에서 `provider_call_count=null`로 기록합니다.
- 직접 호출의 기본 진단 제한시간은 로컬 테스트용 60초입니다. 기존 OpenCode 경로는 30초와 별도 정리 예산 3초를 사용합니다. 설계안의 p95 30초 목표 달성 여부는 측정하지 않았습니다.
- 규칙은 실패 가능성이 있는 줄을 표시하는 보조 신호입니다. 오류 유형별 상세 규칙·원인 정확도 검증은 실제 로그 확보 후 확장합니다.
- 마스킹은 알려진 패턴을 대상으로 하며 임의 형식의 모든 비밀값 탐지를 보장하지 않습니다. 실제 로그 공급원의 마스킹 정책과 사례를 보강해야 합니다.
- 파일 출력은 로컬 진단 기록이며 내구성 있는 작업 큐나 다중 사용자 저장소가 아닙니다. 테넌트·배포 접근 인증, 중복 요청, 재시작 복구, 보존 기간은 2차 서버 단계에 포함합니다.

## 코드 위치

```text
src/ai_error_check_agent/
  agent/              진단 프롬프트와 응답 스키마
  contracts.py        백엔드 로그 입력 규격
  preprocessing.py    정규화·마스킹·근거 매핑
  validation.py       JSON·근거·교차 참조·상태 검사
  remediation.py      상세 해결안·대상·자리표시자 검사와 수정 예시 보호
  runtime.py          OpenCode HTTP 어댑터
  runtime_contract.py 공통 모델 실행 인터페이스
  direct_api.py       .env 설정과 OpenAI Responses API 직접 호출
  service.py          diagnose()와 실행 기록
  cli.py              prepare / validate / diagnose
  evaluate.py         개발 사례 반복 실행·상태·시간 집계
config/               전용 런타임 설정과 모델 프로필 예시
examples/             가상 개발 입력·참조 답변
evaluation/           모델에 전달하지 않는 개발 평가 정답
tests/                입력·진단·실패 처리·CLI 테스트
run_diagnosis.cmd      CMD에서 기본 샘플 또는 지정 로그 진단
```

다음 구현은 실제 로그·실제 모델 연결 검증 → 오류 유형별 개발 평가 → Control API와 작업 저장 연결 순서로 진행합니다.
