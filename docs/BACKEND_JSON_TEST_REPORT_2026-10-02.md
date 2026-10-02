# 백엔드 JSON·S3 소스 입력 테스트 — 2026-10-02

대상: AI_Error_Check_Agent v0.4.0, `POST /diagnose`의 `success/message/data` 입력과 조건부 `.tar.gz` 소스 분석.

## 결과

- 자동 테스트 **223개 통과**. 기존 169개와 신규 54개를 함께 실행했다.
- `ruff check src tests evaluation` 통과.
- 실제 `gpt-6.1-sol`을 사용한 합성 시나리오 **5/5 통과**. 모델 호출은 총 7회였다.
- S3 다운로드는 HTTPX 테스트 전송이 합성 tar.gz를 반환하도록 대체했다. **실제 운영 S3 버킷·백엔드 통합 결과가 아니다.**

## 실제 모델 시나리오

| 시나리오 | 기대한 동작 및 확인 결과 | LLM 호출 | 모의 다운로드 | 소요 시간 |
| --- | --- | ---: | ---: | ---: |
| compile-source | 빌드 로그 분석 → 압축 파일의 관련 TypeScript 코드 분석 → 위치·수정안 제시 | 2 | 1 | 69.875초 |
| runtime-source | 런타임 로그 분석 → SHA가 없는 소스도 분석 → 위치·수정안 제시 | 2 | 1 | 69.562초 |
| configuration-log-only | 요청 예시의 DATABASE_URL 누락 로그를 진단하고 설정 변경·검증 절차 제시 | 1 | 0 | 23.781초 |
| recovered-skip-source | 복구된 상황에서 불필요한 소스 조회 생략 | 1 | 0 | 8.562초 |
| expired-archive | 소스가 필요하지만 URL이 만료된 경우 다운로드 생략, 로그 진단 유지 | 1 | 0 | 25.703초 |

HTTP 상태, 진단 상태, 소스 처리 상태, 다운로드 횟수, 예상 파일 위치, 해결안 유무, 로그 ID 보존, URL 서명 미노출을 검사했다. 코드 분석 사례는 로그 EV·코드 SC 근거를 연결한 결과를 반환했다. TypeScript 사례의 숫자 변환·유효 범위 확인 제안도 수동으로 검토했다. 제안된 수정 코드를 사용자 저장소에 적용하거나 실행한 것은 아니다.

요청별 중앙값은 25.703초, 최댓값은 69.875초였다. 총 입력 토큰 42,554개, 출력 토큰 10,289개로 집계됐다. 작은 합성 시나리오 5건의 단일 실행이므로 운영 정확도나 지연시간 보장으로 해석하지 않는다. 비용 금액은 제공자 응답에 없어 집계하지 않았다.

원본 결과: `results/backend-envelope-20261002-01/summary.json` 및 같은 폴더의 시나리오별 JSON. 이 폴더는 Git에서 제외된다. 실제 모델 실행 이후 추가한 `root_directory`와 타임라인 전처리 버전 표기, 중복 sequence 검증은 최종 자동 테스트로 검증했다.

## 자동 테스트 범위

- 합의한 전체 envelope, `data` 단독 입력, 기존 v0.3 입력의 호환성 및 OpenAPI 스키마.
- 조회 실패(`success=false`), 숫자 ID, 선택/null 메타데이터, 커밋 SHA·소스 생략.
- 로그 출처별 sequence 정렬, timestamp 대체 정렬, 순서 정보 부족 표시, 이벤트·물리 줄 매핑, 누락 범위 표시.
- 20개를 초과하는 로그 이벤트, 조회 시간 범위 검증, 중복 로그 ID·sequence 거절, null/unknown 출처 구분 일관성.
- 여러 이벤트에 걸친 개인키 마스킹, 모델·응답에 presigned URL을 전달하지 않는 처리, HTTPX INFO 로그의 S3 query 마스킹.
- 소스가 필요할 때만 다운로드, 원래 URL query 유지, AWS·LLM 인증 헤더를 S3에 추가하지 않는 처리.
- GitHub 압축 최상위 폴더, 모노레포 rootDirectory, 로그의 저장소 상대 경로, 파일 목록 기반 제한적 대체 선택.
- HTTP 403·리디렉션·만료·시간 초과·잘못된 gzip 및 tar, 다운로드·압축 해제 크기·파일 수 제한.
- 외부/로컬 주소 거절, S3 호스트 제한, 경로 이탈·중복 경로 거절, 링크·특수 파일·바이너리 제외.
- 실제 파일 범위로 끝 줄 제한, 파일 크기 한도, 소스 오류 시 기존 로그 진단 유지.

## 재현

저장소 루트에서 실행한다.

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests evaluation
```

실제 LLM 시나리오를 다시 실행하려면 새 결과 폴더를 지정한다. 아래 명령은 API 사용량이 발생하며 S3 전송은 계속 모의 처리한다.

```bat
.venv\Scripts\python.exe evaluation\run_backend_envelope.py --output-dir results\backend-envelope-new-run
```

실제 S3 URL과 본 서비스 로그를 받은 뒤에는 유효한 presigned URL의 다운로드, 버킷의 HTTP Content-Encoding 설정, 실제 압축 최상위 폴더·rootDirectory, 로그 규모, 서비스의 요청 제한시간을 통합 환경에서 확인해야 한다. 두 번의 모델 호출이 필요한 테스트는 약 70초가 소요됐다.
