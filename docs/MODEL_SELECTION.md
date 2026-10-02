# OpenCode 및 서비스 모델 선택 — v0.5.0

두 실행 방식은 같은 서버 모델 목록을 사용한다. 에이전트 역할은 `iris_diagnosis`이며, 이 역할을 수행할 GPT 또는 Sakana 모델을 선택한다. OpenCode 화면에서의 선택은 해당 대화에만 적용된다. 서비스 요청은 각 요청의 선택값을 사용하므로 다른 사용자의 선택과 섞이지 않는다.

## 설정

기존 `.env`의 `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-6.1-sol`, `LLM_API_KEY`, `LLM_BASE_URL`은 그대로 사용할 수 있다. 추가 설정 예시는 다음과 같다. 실제 키를 저장소에 넣지 않는다.

```dotenv
SAKANA_API_KEY=YOUR_SAKANA_API_KEY
OPENAI_MODELS=gpt-6.1-sol
SAKANA_MODELS=fugu,fugu-max,fugu-ultra,sakana-namazu
SAKANA_REASONING_EFFORT=high
SAKANA_TIMEOUT_SECONDS=120
SAKANA_MAX_OUTPUT_TOKENS=8192
```

`OPENAI_API_KEY`가 있으면 기존 `LLM_API_KEY`보다 우선한다. Sakana에는 반드시 별도 `SAKANA_API_KEY`를 사용하며 OpenAI 키로 대체하지 않는다. 추가 OpenAI 모델의 옵션은 `OPENAI_REASONING_EFFORT`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_OUTPUT_TOKENS`로 설정할 수 있다. 기본 모델의 옵션은 기존 `LLM_*` 설정을 따른다.

각 모델 목록은 쉼표로 구분한 최대 12개 모델 ID다. 모델 ID와 계정 접근 권한은 제공자 문서를 확인한다. 모델 목록 변경과 키 갱신 후 API를 재시작한다. 서버는 고정된 공식 제공자 주소만 허용하고 클라이언트로부터 API 주소·키를 받지 않는다.

Sakana를 기본 모델로 쓰려면 `LLM_PROVIDER=sakana`, `LLM_MODEL=fugu`, `LLM_BASE_URL=https://api.sakana.ai/v1`, `LLM_REASONING_EFFORT=high`와 `SAKANA_API_KEY`를 설정한다. 필요하면 `LLM_TIMEOUT_SECONDS=120`, `LLM_MAX_OUTPUT_TOKENS=8192`도 맞춘다.

## 1. OpenCode 화면

설치된 가상환경과 OpenCode를 사용해 저장소 루트의 CMD에서 실행한다.

```bat
run_opencode.cmd --list
run_opencode.cmd
```

화면에서 `iris_diagnosis`가 기본 에이전트로 선택된다. `/models`를 입력하고 Enter를 눌러 모델을 선택한다. 키가 있는 제공자의 등록 모델만 OpenCode 메뉴에 나타난다. 예를 들어 Sakana 키를 설정하면 `fugu`, `fugu-max`, `fugu-ultra`, `sakana-namazu`를 선택할 수 있다.

```bat
run_opencode.cmd --model sakana/fugu-max
```

이 명령은 시작 모델을 지정한다. 화면에서 다시 바꿀 수 있다. 대화형 OpenCode는 기본 4097 포트를 사용하므로 API 내부의 4096 포트와 함께 실행할 수 있다. 충돌 시 `--port 4098`처럼 바꾼다. `/exit`로 종료하면 실행기가 자신이 시작한 OpenCode 서버를 정리한다.

마스킹한 로그를 붙여 넣고, 필요한 소스 부분을 추가해 대화형으로 원인·수정안·검증 절차를 검토한다. 도구 실행과 파일 변경은 차단된다. 이 화면의 대화는 API의 자동 마스킹·S3 다운로드·두 단계 근거 검증을 통과한 `diagnosis-result.v3` 결과가 아니다. 서비스와 같은 검증 절차가 필요하면 아래 API를 사용한다. 대화 기록은 Git에서 제외된 `.runtime/opencode-runs`에 남을 수 있다.

## 2. 서비스 화면에서 선택

```text
IRIS 프론트 → IRIS 백엔드 → GET /models
                         ← 모델 목록
사용자가 모델 선택
IRIS 프론트 → IRIS 백엔드 → POST /diagnose?model=sakana/fugu-max
                         ← 기존 diagnosis-result.v3 결과
```

프론트는 기존 로그인으로 IRIS 백엔드에 요청한다. 백엔드가 에이전트의 `X-API-Key` 인증을 담당한다. LLM 키와 `AGENT_API_KEY`를 브라우저에 전달하지 않는다. 이 저장소에는 프론트 UI나 IRIS 백엔드 프록시 구현은 포함되어 있지 않다.

```bat
curl.exe http://127.0.0.1:8001/models -H "X-API-Key: YOUR_AGENT_API_KEY"
curl.exe -X POST "http://127.0.0.1:8001/diagnose?model=sakana/fugu-max" -H "X-API-Key: YOUR_AGENT_API_KEY" -H "Content-Type: application/json" --data-binary "@examples/backend-log-only.request.json"
```

모델 목록 응답 예시:

```json
{
  "defaultModel": "openai/gpt-6.1-sol",
  "agent": "iris_diagnosis",
  "models": [
    {"id": "openai/gpt-6.1-sol", "provider": "openai", "model": "gpt-6.1-sol", "available": true, "unavailableReason": null},
    {"id": "sakana/fugu", "provider": "sakana", "model": "fugu", "available": false, "unavailableReason": "missing_api_key"}
  ]
}
```

`available`은 서버에 키가 설정됐다는 뜻이다. 키 유효성·과금 잔액·모델 접근 권한·제공자 상태까지 확인했다는 뜻은 아니다. 프론트는 `available=false` 항목을 비활성화하면 된다.

기존 JSON 본문은 그대로 전달하고 선택 모델 ID만 `model` 쿼리에 추가한다. `success/message/data`, data만 보낸 형식, `diagnosis/source_snapshot` 형식을 모두 지원한다. `model`을 생략하면 서버의 기존 기본 모델을 사용한다. 명시한 모델은 로그 단계와 소스 단계에 고정되고, 실행 결과 `execution.stages[].requested_model` 및 `reported_model`에서 확인할 수 있다.

| 상태 | 의미 |
| --- | --- |
| 401 | 에이전트 인증 키 오류 |
| 422 `UNKNOWN_MODEL` | 등록되지 않은 모델 또는 빈 선택값 |
| 503 `MODEL_NOT_CONFIGURED` | 선택한 모델의 서버 키 없음 |
| 429 | 동시 진단 한도 초과 |
| 502/503/504 | 모델 실행·연결·시간 제한 오류. 응답의 error.code 확인 |

실패 시 다른 모델로 자동 전환하지 않는다. 키가 있는 등록 모델의 호출 권한은 IRIS 백엔드에서 사용자·프로젝트별로 제한할 수 있다. 요청에서 제공자 URL, 키, 임의 에이전트나 실행 권한을 변경할 수 없다.

## EKS와 Docker

동일 이미지에 서버 API, OpenCode, 공통 모델 목록과 대화형 실행 모듈이 포함된다. EKS에서는 `AGENT_RUNTIME=opencode`, 기본 `LLM_*`, `AGENT_API_KEY`, `SAKANA_API_KEY`를 환경변수로 설정한다. 모델 키는 Secret으로 주입한다. OpenCode의 4096 포트를 외부로 공개할 필요는 없다.

Sakana의 기본 제한시간은 단계당 120초다. 두 단계와 소스 다운로드·정리를 고려해 실행 예시의 종료 유예는 280초로 설정했다. 백엔드 요청 제한시간, ingress 제한시간, Kubernetes `terminationGracePeriodSeconds`도 해당 처리 시간에 맞춘다. 직접 호출 모드에서 더 긴 시간을 설정한다면 함께 조정한다.

## 검증 범위

동시 요청의 두 단계 모델 유지, 키 누락과 알 수 없는 모델 차단, 제공자별 키 분리, 기존 요청 호환성을 자동 테스트로 확인한다. 실제 OpenCode의 `/models` 화면 선택과 Sakana 4종의 Responses 전송은 로컬 프로토콜 테스트 서버로 확인했다. 기존 GPT 키로는 선택 모델을 지정한 실제 진단을 수행했다. 실제 Sakana 계정의 추론 결과·진단 품질은 유효한 Sakana 키를 주입한 후 검증해야 한다.

공식 참고: [OpenCode 모델 선택](https://opencode.ai/docs/models/), [커스텀 제공자](https://opencode.ai/docs/providers/#custom-provider), [Sakana 모델·지원 API](https://console.sakana.ai/models).
