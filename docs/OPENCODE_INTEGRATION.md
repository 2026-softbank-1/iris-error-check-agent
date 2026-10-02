# OpenCode 실행 방식 — v0.5.0

v0.5.0부터 `run_opencode.cmd`의 대화형 `/models` 선택과 서비스 API의 요청별 모델 선택을 지원한다. 공통 모델 목록, 키 설정, 사용법은 [모델 선택 문서](MODEL_SELECTION.md)를 참고한다.

에이전트 API에서 OpenAI 직접 호출과 OpenCode 경유 호출을 선택할 수 있다. 두 방식 모두 같은 `POST /diagnose` 요청·응답을 사용하며, 합의한 백엔드 JSON과 S3 tar.gz 소스 분석을 지원한다.

## 설치와 실행

저장소 루트의 CMD에서 실행한다. Python 가상환경과 기존 `.env`, Node.js/npm, Git이 필요하다.

```bat
install_opencode.cmd
run_api.cmd --runtime opencode --dev
```

설치 명령은 공식 `opencode-ai@1.18.34`를 `.runtime/opencode-tooling`에 설치한다. 버전을 고정하며 전역 PATH에 `opencode`를 추가하지 않는다. API를 종료하면 자신이 시작한 OpenCode 프로세스도 종료한다. CMD에서는 Ctrl+C로 종료한다.

다른 CMD에서 호출한다. 실제 LLM 사용량이 발생한다.

```bat
curl.exe -X POST http://127.0.0.1:8001/diagnose -H "Content-Type: application/json" --data-binary "@examples/backend-log-only.request.json"
```

직접 호출로 실행하려면 `run_api.cmd --runtime direct --dev`를 사용한다. 실행 옵션을 생략했을 때의 기본값은 `.env`의 `AGENT_RUNTIME`이며, 값이 없으면 `direct`다. `--runtime`은 `.env`보다 우선한다. `run_diagnosis.cmd`의 기존 CLI 동작은 바뀌지 않는다.

서버 모드는 `AGENT_API_KEY`를 설정하고 `--dev` 없이 실행한다. 외부 백엔드가 호출해야 하면 `--host 0.0.0.0`을 지정하며, 요청의 `X-API-Key`로 인증한다. 내부 OpenCode는 계속 `127.0.0.1`에서만 실행된다.

## 설정

| 설정 | 동작 |
| --- | --- |
| `LLM_API_KEY` | 기존 키를 OpenCode 자식 프로세스의 `OPENAI_API_KEY`로 전달. 생성 설정 파일이나 명령 인자에 저장하지 않음 |
| `LLM_MODEL` | 지정한 모델 ID를 그대로 사용. 검증 모델은 gpt-6.1-sol |
| `LLM_BASE_URL` | 기본 제공자와 일치하는 공식 OpenAI 또는 Sakana 주소만 허용 |
| `SAKANA_API_KEY` | Sakana 모델 전용 키. OpenAI 키를 대신 보내지 않음 |
| `OPENAI_MODELS` / `SAKANA_MODELS` | 화면과 API에 등록할 모델 ID 목록. 쉼표 구분 |
| `LLM_REASONING_EFFORT` | OpenCode 모델 옵션의 reasoningEffort에 적용 |
| `LLM_MAX_OUTPUT_TOKENS` | OpenCode 모델의 출력 상한에 적용 |
| `LLM_TIMEOUT_SECONDS` | 각 진단 단계의 제한시간. OpenCode 경로는 최대 120초 |
| `--opencode-port` | 내부 OpenCode 포트, 기본 4096 |
| `--port` | 에이전트 API 포트, 기본 8001 |

4096 포트를 이미 사용 중이면 기존 프로세스에 연결하거나 종료하지 않고 시작 오류를 반환한다. 예를 들어 `run_api.cmd --runtime opencode --opencode-port 4097 --dev`로 다른 포트를 지정할 수 있다.

## 실행 구조

```text
백엔드 JSON → 에이전트 API → 마스킹한 로그 → OpenCode → 설정된 LLM
                       └ 필요할 때 S3 tar.gz 조회
                         → 관련 코드 선택·마스킹 → 새 OpenCode 세션 → LLM
                       ← 검증된 원인·해결안·코드 위치·검증 절차
```

S3 다운로드와 파일 선택은 에이전트 API가 수행한다. OpenCode에는 필요한 로그·코드 근거만 전달하며, presigned URL은 전달하지 않는다. OpenCode의 셸·파일 수정·웹 도구 등은 사용하지 않는다. 제안한 수정의 실제 적용이나 재배포 기능은 포함하지 않는다.

매번 `.runtime/opencode-runs/<실행 ID>` 아래에 별도 설정·상태·캐시·홈·빈 Git 작업 폴더를 만든다. 프로젝트·개인 설정 로딩을 차단하고 `--pure`로 실행한다. 자식 프로세스에는 OS 실행에 필요한 환경과 명시한 모델 키만 전달한다. 내부 HTTP 서버에는 실행할 때마다 생성한 비밀번호를 적용하며, API 프로세스 메모리에서만 관리한다.

이는 설정과 작업 폴더의 분리이며 OS 수준의 샌드박스는 아니다. `.runtime`은 Git에서 제외되며 실행 기록·DB·캐시는 로컬에 남을 수 있다. 정상 진단 후 생성한 세션은 삭제한다. 프로세스 종료나 운영 환경의 강제 종료까지 제공자 측 생성 취소를 보장하지는 않는다.

실행 어댑터는 버전, 전용 에이전트, 도구 차단, 플러그인·MCP·추가 지침, 공유·스냅샷·자동 업데이트·자동 압축·제목/요약 에이전트 비활성화를 확인한다. 각 진단 단계마다 새 세션을 만들고 결과 모델 ID와 근거를 검사한다. 실패·시간 초과는 중단 요청과 세션 정리 상태로 기록한다.

## 검증 및 한계

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe evaluation\run_backend_envelope.py --runtime opencode --output-dir results\opencode-new-run
```

두 번째 명령은 실제 OpenCode와 LLM을 호출한다. 로그·소스는 합성 자료이며 S3 HTTP 전송은 테스트 응답으로 대체한다. 실제 운영 버킷과 백엔드 로그 연결은 별도 확인이 필요하다.

`execution.stages[].runtime_version`은 OpenCode 버전, `message_submissions`는 제출한 진단 메시지 수다. OpenCode 내부 재시도까지 계측하지 않으므로 `provider_call_count`는 null로 유지한다. OpenCode가 보고하는 입력 토큰에는 캐시 읽기 토큰이 별도로 계산될 수 있어 직접 호출 수치와 단순 비교하지 않는다. 구조화 출력은 JSON Schema를 프롬프트로 전달하고 에이전트에서 검증하는 방식이다.

실제 검증 결과는 [2026-10-02 테스트 보고서](OPENCODE_TEST_REPORT_2026-10-02.md)에 있다.

공식 참고 자료: [설치](https://opencode.ai/docs/), [HTTP 서버](https://opencode.ai/docs/server/), [설정](https://opencode.ai/docs/config/). 설정 분리는 설치 버전의 [Global 경로 구현](https://github.com/anomalyco/opencode/blob/v1.18.34/packages/core/src/global.ts)과 [Config 경로 구현](https://github.com/anomalyco/opencode/blob/v1.18.34/packages/opencode/src/config/paths.ts)을 기준으로 확인했다.
