# OpenCode 실제 연동 검증 — 2026-10-02

대상: AI_Error_Check_Agent v0.4.1, Windows의 프로젝트 전용 OpenCode 1.18.34, 기존 `.env`에 설정된 gpt-6.1-sol.

## 결과

- 자동 테스트 **241개 통과**. 기존 223개에 실행 방식 선택·프로세스 정리·설정 분리·OpenCode 소스 분석·사전 검사 등 18개를 추가했다.
- Ruff 검사 통과.
- 실제 OpenCode와 LLM을 통한 단일 로그 진단 통과. 결과는 HTTP 200, diagnosed였고 세션 목록은 0개로 정리됐다.
- 최종 실제 모델 시나리오 **5/5 통과**. 로그 및 소스 분석을 위해 총 7개의 진단 메시지를 제출했다. OpenCode 내부 제공자 호출 횟수는 별도로 계측하지 않아 `provider_call_count=null`을 유지한다.
- 실제 TCP API의 스키마·요청 처리, 내부 인증, 빈 작업 폴더 분리, API·OpenCode 종료 검사 9개 통과.

## 실제 모델 시나리오

| 시나리오 | 확인 결과 | 진단 메시지 | 모의 S3 다운로드 | 소요 시간 |
| --- | --- | ---: | ---: | ---: |
| compile-source | src/server.ts의 string→number 할당 오류를 소스와 연결하고 숫자 변환·범위 검증 코드 제시 | 2 | 1 | 65.734초 |
| runtime-source | SHA가 없어도 src/config.py를 분석해 배포 계약과 DATABASE_URl 조회 키의 대소문자 불일치 확인 | 2 | 1 | 65.047초 |
| configuration-log-only | 필수 설정 누락을 로그만으로 진단, 소스 조회 생략 | 1 | 0 | 25.640초 |
| recovered-skip-source | 복구·성공 로그에 불필요한 코드 조회 생략 | 1 | 0 | 10.281초 |
| expired-archive | 소스 URL 만료를 표시하고 유효한 로그 진단 유지 | 1 | 0 | 28.500초 |

요청별 중앙값은 28.500초, 최댓값은 65.734초였다. 두 단계가 필요한 진단은 약 65초였으므로 백엔드·프록시 요청 제한시간을 함께 맞춰야 한다.

각 사례에서 HTTP·진단·소스 상태, 기대 파일 위치, 상세 해결안, 로그 ID 보존, presigned URL 서명 미노출, 진단 세션 삭제·실행기 재사용 상태를 확인했다. 실제 코드 분석 결과의 수정 예시도 수동 검토했다. 제안된 코드를 적용하거나 재배포하지 않았다.

## 발견한 문제와 보완

첫 5개 시나리오 실행은 **4/5 통과**였다. 런타임 사례에서 모델은 잘못된 환경변수 이름 참조와 실제 설정 누락을 구분하지 못한 채, 추가 소스 확인을 생략했다. 소스 제공 여부나 SHA 파싱 오류가 아니라 조회 필요성 판단의 문제였다.

설정 누락 메시지와 파일·줄 번호가 있는 코드 참조 오류를 구분하도록 공통 소스 선택 지침을 보완했다. 코드의 오타·대소문자·잘못된 이름 참조가 가능한 경우 관련 소스로 선언·주변 문맥을 확인하고, 실제 배포 설정은 별도 확인 사항으로 남기도록 했다. 기대 결과를 완화하지 않고 동일한 5개 시나리오를 재실행하여 5/5를 확인했다.

## 검증 자료

- `results/opencode-first-20261002/configuration.json`: 최초 실제 OpenCode 단일 진단.
- `results/opencode-backend-20261002-01/summary.json`: 보완 전 4/5 결과.
- `results/opencode-backend-20261002-02/summary.json`: 최종 5/5 결과와 단계별 시간·모델·사용량·프롬프트 해시.
- `results/opencode-http-smoke-20261002.json`: 실제 TCP API 및 프로세스 정리 9개 검사 결과.

`results/`와 설치·실행 상태가 있는 `.runtime/`은 Git에서 제외한다. LLM 키는 기존 `.env`를 읽어 자식 프로세스 환경에 전달하며 `.env` 자체는 변경하지 않았다.

## 범위와 한계

OpenCode와 LLM은 실제 실행했지만, 로그·코드는 합성 데이터이고 S3는 합성 tar.gz를 반환하는 HTTPX 테스트 전송으로 대체했다. 따라서 실제 버킷 권한·네트워크·백엔드 로그 수집은 검증 대상에 포함하지 않았다.

최종 실제 모델 사례 5건의 통과율은 개발 시나리오 결과이며, 운영 장애 정확도나 반복 실행의 성공률을 의미하지 않는다. OpenCode가 보고한 토큰·비용 값은 제공자 청구서 검증 수치가 아니다. 설정·작업 폴더 분리와 도구 차단은 확인했으며 OS 샌드박스 수준의 격리는 제공하지 않는다.

실행 명령과 구조는 [OpenCode 연동 문서](OPENCODE_INTEGRATION.md)를 참고한다. 자동 테스트는 비용 없이 실행할 수 있고, 실제 모델 평가는 아래 명령으로 새 결과 폴더에 재현한다.

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe evaluation\run_backend_envelope.py --runtime opencode --output-dir results\opencode-new-run
```
