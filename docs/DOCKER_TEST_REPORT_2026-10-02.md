# Docker 검증 기록 — 2026-10-02

## v0.5.0 재빌드 및 모델 선택 검증

현재 저장소의 코드를 다시 빌드하여 `iris-error-check-agent:0.5.0`과 `iris-error-check-agent:latest`에 동일한 이미지를 지정했다. 기존 빌드와 코드가 같아 Docker 캐시가 재사용되었다. Dockerfile 수정 없이 최신 코드가 포함되며, 실제 컨테이너에 설치된 모든 Python 소스와 에이전트 리소스의 SHA-256을 현재 저장소와 비교해 일치를 확인했다.

- 플랫폼: `linux/amd64`, 애플리케이션 `0.5.0`, OpenCode `1.18.34`
- Linux 이미지 manifest: `sha256:c36b6c07d70e0880a143e6eced2e307734e39ec56c410f888aa9981b4cff2e04`
- 로컬 이미지 ID: `sha256:44c5c4b8b1d0a5bdb7abd1937e5e23c07baa4ac82c81f4a7578adc90c599686b`
- 이번 검증은 더미 키만 사용했으며 실제 LLM 진단 호출은 0회다.

| 항목 | 결과 |
| --- | --- |
| OpenCode / direct 모드 컨테이너 기동 | 모두 통과 |
| `/healthz` 버전 | HTTP 200, `0.5.0` |
| `/models` | 기본 GPT와 Sakana 4종, `iris_diagnosis` 확인 |
| 제공자 설정 | OpenAI·Sakana의 공식 API 주소와 두 제공자 등록 확인 |
| 인증 없는 `/models`, `/diagnose` | HTTP 401 |
| 등록되지 않은 모델 / 실패한 백엔드 JSON | HTTP 422 |
| Sakana 키가 없는 상태에서 Sakana 선택 | HTTP 503 |
| 응답에서 인증 키 제외 | 통과 |
| 설치된 코드·리소스와 저장소의 해시 비교 | 전부 일치, 대화형 프롬프트 포함 |
| `pip check` / OpenCode 버전 확인 | 통과 / `1.18.34` |
| 비루트 실행·파일 제외·임시 폴더 쓰기 권한 | 통과 |
| Docker HEALTHCHECK | healthy |
| 두 모드의 정상 종료 | ASGI shutdown 완료, 종료 코드 0, OOM 없음 |
| 기존 `.env` | 실행 전후 해시 동일 |

`available`은 해당 제공자의 키가 설정되었다는 뜻이며, 더미 키로 실제 제공자의 인증·잔액·추론 성공을 검증한 것은 아니다. 실제 Sakana 추론, 실제 백엔드·S3 연동, arm64, ECR 푸시와 EKS 배포는 이번 검증 범위에 포함하지 않았다. 테스트 컨테이너는 제거하고 빌드 이미지만 로컬에 남겼다.

로컬 원본 결과: `results/docker-rebuild-v050-9c21ce85/summary.json` 및 모드별 로그. 이 폴더는 Git에서 제외된다. v0.5.0의 앞선 모델 선택 검증은 [모델 선택 검증 기록](MODEL_SELECTION_TEST_REPORT_2026-10-02.md)을 참고한다.

아래는 기존 v0.4.2 이미지의 실제 GPT 진단 및 종료 처리 검증 기록이다.

## 구성과 범위

- 애플리케이션: `0.4.2`
- 이미지 태그: `iris-error-check-agent:0.4.2`
- 플랫폼: Docker Desktop Linux 엔진, `linux/amd64`
- 최종 Linux 이미지 manifest: `sha256:4c4dc8890ee030a86b3dfa7f64cec2cf5653cfa0ead20e1295695f3ba891e80f`
- Python 3.12 / Debian Bookworm, OpenCode `1.18.34`
- 모델: 기존 `.env`로 설정한 `openai/gpt-6.1-sol`

API와 OpenCode를 단일 이미지에 포함해 빌드하고 실제 컨테이너의 HTTP 포트로 검증했다. 배포는 로컬 Docker까지이며 ECR 푸시, EKS 배포, 실제 백엔드·S3 연결은 수행하지 않았다. arm64는 검증하지 않았다.

## 결과

| 항목 | 결과 |
| --- | --- |
| Docker 이미지 빌드 및 `pip check` | 통과 |
| 자동 테스트 | 245개 통과 |
| Ruff 코드 검사 | 통과 |
| 비루트 실행 | UID/GID 10001 확인 |
| `GET /healthz` | HTTP 200, 버전 0.4.2 |
| 인증 키 없는 `POST /diagnose` | HTTP 401 |
| 실패한 백엔드 응답 데이터 입력 | HTTP 422 |
| Docker HEALTHCHECK | healthy |
| 비공개 파일 제외 | 이미지 `/app`에 `.env`, `.git`, `.venv`, 테스트 결과 없음 |
| SIGTERM 종료 | ASGI shutdown 완료, 컨테이너 종료 코드 0, OOM 없음 |
| 기존 `.env` | 실행 전후 해시 동일 |

## 실제 모델 진단

합성 데이터로 다음 두 요청을 실행했다. OpenCode에 제출한 메시지는 로그 진단 2회와 소스 분석 1회로 총 3회다. 제공자 내부 호출 횟수는 측정하지 않았다.

| 시나리오 | 입력 | 응답 | 소스 분석 | HTTP 왕복 시간 |
| --- | --- | --- | --- | --- |
| 환경변수 누락 | 백엔드 `success/message/data` JSON, `DATABASE_URL` 오류, 소스 없음 | diagnosed / HTTP 200 | not_needed | 27.92초 |
| TypeScript 빌드 실패 | 로그 및 인라인 `src/server.ts` 파일 | diagnosed / HTTP 200 | analyzed | 61.20초 |

두 요청 모두 변경 제안·수정 후 검증·되돌리기 절차를 포함했다. 소스 시나리오에서는 `src/server.ts`의 문제 위치와 해당 파일을 대상으로 한 수정안을 반환했다. 관계없는 파일의 canary는 응답에 없었고, 실제 코드 변경이나 재배포는 실행하지 않았다. 세 단계 모두 OpenCode 세션 삭제를 확인했다.

이는 두 개 합성 시나리오의 실행 확인이며 운영 정확도나 성능 보장 수치가 아니다. 이번 Docker 테스트에서는 실제 S3 presigned URL 다운로드를 수행하지 않았다.

## 종료 처리 보완과 재검증

첫 실행에서는 진단 2건이 통과했지만, 종료 코드 0을 기대한 검사에서 실패했다. 설치된 Uvicorn은 Unix에서 정상 shutdown 후 SIGTERM을 다시 발생시키므로, `uvicorn.run()` 바깥의 `finally`만으로는 OpenCode 정리를 보장할 수 없었다.

OpenCode 시작과 정리를 API의 ASGI lifespan으로 옮겼다. 정상 종료, 시작 실패, 서비스 중 예외에서 정리되는 회귀 테스트를 추가했다. 이미지에서는 SIGTERM 코드 143만 `tini -e 143`으로 0에 매핑하며, 강제 종료 137 및 다른 오류 코드는 유지한다. 관련 동작은 [Uvicorn lifespan](https://uvicorn.dev/concepts/lifespan/)과 [tini 종료 코드 설정](https://github.com/krallin/tini#remapping-exit-codes)을 따른다.

최종 이미지에서 기동·인증·HEALTHCHECK·SIGTERM 종료를 다시 확인했고 모두 통과했다. 종료 처리 외에 진단 로직은 변경하지 않았으므로 유료 진단 요청은 반복하지 않았다. 최종 종료 로그에 `Application shutdown complete.`가 기록되었으며, 테스트 컨테이너는 정리하고 이미지는 로컬에 남겼다.

로컬 원본 결과는 Git에서 제외되는 다음 폴더에 있다.

- `results/iris-agent-smoke-62318341/`: 실제 진단 2건과 첫 종료 검사 결과
- `results/iris-agent-smoke-790ed028/`: 최종 이미지 상태·인증·종료 검증 및 컨테이너 로그
