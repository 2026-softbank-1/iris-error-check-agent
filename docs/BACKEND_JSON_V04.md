# 백엔드 JSON·S3 소스 연동 — v0.4

`POST /diagnose`에 팀에서 정한 `success/message/data` JSON 전체를 그대로 보냅니다. `data`만 보내는 방식과 기존 v0.3 `diagnosis/source_snapshot` 입력도 지원합니다. 응답은 기존 `diagnosis-result.v3` 구조를 유지하면서 `backend_context`, 소스 아카이브 해시·루트 경로, 로그별 타임라인 정보를 추가합니다.

## 호출

```bat
run_api.cmd --dev
```

다른 CMD에서 로그 전용 합성 요청을 실행합니다. 실제 LLM 사용량이 발생합니다.

```bat
curl.exe -X POST http://127.0.0.1:8001/diagnose -H "Content-Type: application/json" --data-binary "@examples/backend-log-only.request.json"
```

전체 소스 메타데이터 예시는 [`examples/backend-envelope.request.json`](../examples/backend-envelope.request.json)입니다. 예시의 `<S3 presigned URL>`과 만료 시각을 실제 값으로 교체해야 소스를 다운로드할 수 있습니다. 소스가 필요 없는 진단은 해당 URL을 사용하지 않습니다.

서버 모드는 기존과 같이 `AGENT_API_KEY`를 설정하고 `run_api.cmd --host 0.0.0.0`으로 실행하며, 요청에 `X-API-Key`를 넣습니다. LLM 키는 에이전트 서버에서만 사용합니다.

## 요청 계약

```json
{
  "success": true,
  "message": "진단용 데이터 조회 완료",
  "data": {
    "projectId": "project-123",
    "serviceId": "service-456",
    "deploymentId": "deploy-789",
    "attemptId": "attempt-1",
    "deploymentStatus": "FAILED",
    "failedStage": "runtime",
    "exitCode": 1,
    "logRange": {
      "from": "2026-10-02T07:00:00Z",
      "to": "2026-10-02T07:05:00Z",
      "isComplete": true
    },
    "logs": [
      {
        "id": "log-001",
        "timestamp": "2026-10-02T07:01:00Z",
        "stage": "runtime",
        "sourceId": "app-pod-001",
        "stream": "stderr",
        "sequence": 1,
        "text": "ERROR Missing required configuration: DATABASE_URL"
      }
    ],
    "source": {
      "format": "tar.gz",
      "downloadUrl": "<S3 presigned URL>",
      "expiresAt": "2026-10-02T07:20:00Z",
      "commitSha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "rootDirectory": "."
    }
  }
}
```

| 필드 | 처리 |
| --- | --- |
| `success` | 백엔드의 자료 조회 성공 여부. `false`이면 모델·S3를 호출하지 않고 HTTP 422 `UPSTREAM_DATA_UNAVAILABLE` |
| `message` | 표시용 메시지. 선택/null 허용, 모델 입력에 포함하지 않음 |
| `data` | 성공 응답에는 필수. 실패 응답은 null 가능 |
| `projectId/serviceId/deploymentId/attemptId` | 문자열 ID 또는 0 이상의 정수. 응답에서는 문자열로 보존 |
| `deploymentStatus` | 실패·성공·진행 상태를 내부 규격으로 매핑하고 원문 상태도 보존 |
| `failedStage` | `build/release/deploy/runtime/unknown` 또는 null |
| `exitCode` | 정수 또는 null/생략. 알 수 없으면 추측하지 않음 |
| `logRange.from/to` | 시간대 포함 ISO 8601 또는 null/생략. 시작≤종료, 알려진 로그 시각이 조회 범위 밖이면 거절 |
| `logRange.isComplete` | 필수 boolean. 조회 범위 내 누락·발췌·잘림이 있으면 false |
| `logs` | 1~2,000개 이벤트. 각 이벤트의 `id`, `sourceId`, `text` 필수 |
| `timestamp` | 시간대 포함 ISO 8601 또는 null/생략 |
| `stage/stream` | 소문자·대문자 허용. null/생략은 unknown; stream은 stdout/stderr/combined/unknown |
| `sequence` | 0 이상의 정수 또는 null/생략. 같은 sourceId/stage/stream에서 중복 불가 |
| `source` | 생략/null이면 로그만 진단 |
| `source.downloadUrl` | 원본 `.tar.gz`를 GET할 수 있는 HTTPS S3 URL. query를 수정하지 않고 호출 |
| `source.expiresAt` | 시간대 포함 ISO 8601 또는 null/생략. 제공된 시각이 지났으면 다운로드 생략 후 만료 표시 |
| `source.commitSha` | 40자리 Git 커밋 SHA 또는 null/생략. S3 조회 키가 아닌 참고 메타데이터 |
| `source.rootDirectory` | 저장소 루트 기준 상대 경로. 기본/null은 `.`, 예: `apps/api` |

`deploymentStatus`는 `FAILED/CRASHED`→failed, `SUCCEEDED`→succeeded, `RUNNING/QUEUED/INITIALIZING/BUILDING/DEPLOYING`→running으로 변환합니다. `ROLLED_BACK/MANUAL_INTERVENTION/SUPERSEDED/UNKNOWN/null`은 성공·실패를 단정하지 않고 unknown으로 처리합니다. 원래 상태는 `backend_context.deployment_status`에 있습니다.

새 입력에는 tenant/model 설정이 없으므로 서버의 `AGENT_BACKEND_TENANT_ID`(기본 iris), `AGENT_MODEL_PROFILE_ID`(기본 profile-demo-a), `AGENT_MODEL_SETTINGS_VERSION`(기본 backend.v1)을 사용합니다. tenant 값은 내부 범위 표시이며 사용자·팀 권한을 증명하지 않습니다. 본 서비스에서 배포 소유권과 호출 권한을 확인해야 합니다.

## 로그 순서와 근거

같은 `sourceId/stage/stream`의 이벤트를 묶습니다. 모두 sequence가 있으면 sequence순, 그렇지 않고 모두 timestamp가 있으면 시간순으로 정렬합니다. 두 정보가 부족하면 입력 목록 순서를 유지하고 한계를 표시합니다. 서로 다른 출처의 sequence는 비교하지 않습니다.

로그 원문의 줄바꿈을 보존하고 출처별로 마스킹합니다. 여러 이벤트에 걸친 개인키도 같은 출처 안에서 마스킹합니다. `evidence`에는 기존 EV 근거 필드와 함께 `log_id`, UTC `timestamp`, `sequence`, `event_line`이 추가됩니다. `event_line`은 원래 로그 이벤트 안의 1-based 줄 번호입니다. 순서 번호를 실제 소스 파일 줄 번호처럼 사용하지 않습니다.

HTTP 본문은 최대 1 MiB, 로그 원문 합계는 1 MiB/10,000줄이며, 모델에 전달하는 **마스킹 후 로그+메타데이터 JSON은 기본 16 KiB**입니다. 목록 길이가 허용되어도 모델 입력 예산을 넘으면 HTTP 422 `INPUT_TOO_LARGE`가 납니다. 에이전트는 오래된 로그를 몰래 버리지 않으므로, 백엔드에서 오류 전후의 적절한 시간 범위를 조회하고 누락 여부를 표시합니다.

## S3 아카이브 처리

1. 첫 모델 호출은 로그와 소스 아카이브 제공 여부만 받습니다. 다운로드 URL·서명·압축 파일 내용은 전달하지 않습니다.
2. 소스가 필요하면 로그에 나타난 경로와 범위를 요청합니다. 파일을 특정하지 못하면 압축 목록에서 로그에 언급된 파일과 일반 설정 파일을 제한적으로 선택합니다.
3. 서버가 S3 presigned URL로 압축 파일을 다운로드합니다. AWS 키나 LLM 키를 이 요청에 추가하지 않습니다.
4. 압축 데이터의 크기를 제한하며 임시 스트림에서 읽습니다. 저장소 파일을 실행 경로에 풀거나 실행하지 않습니다.
5. GitHub가 추가한 `owner-repo-<7~40자리 hex>` 최상위 폴더를 인식해 제거한 뒤 `rootDirectory` 아래 파일만 선택합니다. 일반 압축의 `src/` 같은 임의의 단일 폴더는 제거하지 않습니다.
6. 요청된 파일만 읽고 비밀값을 마스킹해 두 번째 모델 호출에 전달합니다. 모델이 요청한 끝 줄이 파일 길이를 넘으면 실제 마지막 줄로 제한하고 한계를 표시합니다.

경로는 프로젝트 루트 기준으로 반환됩니다. `source_analysis.root_directory`를 함께 사용하면 저장소 내 위치를 복원할 수 있습니다. 로그에 저장소 기준 경로가 나오면 해당 rootDirectory 접두사를 제거해 조회할 수 있습니다.

| 제한 | 기본값 |
| --- | --- |
| 다운로드 | 20초, 압축 데이터 32 MiB |
| 압축 해제 데이터 | 128 MiB, 최대 5,000개 항목 |
| 읽을 소스 | 최대 3개, 파일별 UTF-8 64 KiB |
| 모델에 전달할 코드 | 근거 JSON 합계 8 KiB, 선택 범위별 최대 120줄 |
| 소스 범위 미지정 시 | 후보 파일 최대 3개, 파일별 앞 40줄 |
| 모델 호출 | 최대 2회. 기존 호출별 시간 제한 적용, 자동 재시도 없음 |

HTTPS AWS S3 호스트와 443 포트만 허용하고 리디렉션은 따르지 않습니다. 압축 파일은 HTTP `Content-Encoding` 없이 원본 gzip 바이트로 제공해야 합니다. URL이 아니라 객체 자체가 `.tar.gz`인 형태입니다. `.env`, 키 파일, `.git`, node_modules·vendor·가상환경·빌드 산출물은 제외합니다. 경로 이탈·중복 경로는 거절하며 링크·특수 파일을 따라가지 않습니다. HTTPX INFO 로그에서도 S3 URL의 query를 마스킹합니다.

운영에서 버킷 호스트를 더 좁히려면 아래 설정을 추가합니다. 기존 `.env`는 구현 과정에서 변경하지 않습니다.

```dotenv
AGENT_SOURCE_ALLOWED_HOSTS=iris-bucket.s3.ap-northeast-2.amazonaws.com
```

비어 있으면 표준 AWS S3 HTTPS 호스트를 허용합니다. 여러 호스트는 쉼표로 구분합니다. 다운로드에 사용할 URL·만료 시각은 백엔드가 진단 시점에 제공하며, 실제 서명의 유효성은 S3가 판단합니다.

## 결과와 오류

에이전트 응답은 공통 `success/data` 봉투로 바꾸지 않고 기존 진단 결과를 반환합니다. 본 서비스의 응답 봉투는 백엔드에서 감쌀 수 있습니다.

- `source_analysis.status=not_needed`: 추가 코드 확인 불필요. S3 호출 없음.
- `analyzed`: 선택한 소스 분석 완료. `findings`에 파일·줄·로그 EV/코드 SC 근거 연결.
- `unavailable`: 소스 미제공, 관련 파일 없음, 읽을 수 있는 파일 없음 등.
- `failed`: 다운로드·만료·압축 형식·모델 코드 분석 실패. **유효한 로그 진단은 유지하고 HTTP 200**, `source_analysis.error`에 원인 표시.

새 소스 오류에는 `SOURCE_URL_NOT_ALLOWED`, `SOURCE_URL_EXPIRED`, `SOURCE_ACCESS_DENIED`, `SOURCE_DOWNLOAD_FAILED`, `SOURCE_TIMEOUT`, `SOURCE_TOO_LARGE`, `SOURCE_TOO_MANY_FILES`, `UNSAFE_ARCHIVE`, `INVALID_ARCHIVE` 등이 있습니다. 오류에 URL 서명이나 공급자 응답 원문을 넣지 않습니다.

`commit_verification=caller_supplied`는 백엔드 제공 SHA가 있다는 뜻입니다. 독립적으로 Git 커밋과 압축 내용을 비교하지 않습니다. SHA가 없으면 `not_supplied`입니다. `archive_sha256`은 실제 다운로드 바이트의 SHA-256이며 Git 커밋 SHA와 다릅니다. 원래 배포 상태와 진단 상태를 혼동하지 않습니다.

## 검증과 한계

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests evaluation
.venv\Scripts\python.exe evaluation\run_backend_envelope.py --output-dir results\backend-new-run
```

마지막 명령은 실제 LLM을 사용하지만 **S3 응답은 합성 tar.gz를 반환하는 테스트 전송으로 대체**합니다. 실제 운영 버킷 URL과 본 서비스 로그가 아직 제공되지 않아 실제 S3·본 서비스 통합은 검증하지 않았습니다. 최종 테스트 결과는 [v0.4 테스트 보고서](BACKEND_JSON_TEST_REPORT_2026-10-02.md)를 참고하세요.
