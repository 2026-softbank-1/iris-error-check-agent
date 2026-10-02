# 상세 해결안 제공 — v0.2

로그 진단 결과에 **수정 코드·설정 변경안·수정 후 검증 절차**를 추가했습니다. 기존 `run_diagnosis.cmd` 실행 시 함께 반환되며 별도 환경변수 설정은 필요하지 않습니다.

## 제공하는 내용

`analysis.remediation.plans`에서 다음 내용을 확인합니다.

| 필드 | 내용 |
| --- | --- |
| `hypothesis_ids`, `evidence_ids` | 어떤 원인과 로그를 근거로 제안했는지 |
| `apply_when` | 해당 수정을 적용할 수 있는 조건 |
| `changes[].kind` | `code`, `configuration`, `command` 중 필요한 변경 종류 |
| `changes[].target`, `target_known` | 수정 대상 및 로그에서 그 대상 문자열이 확인됐는지 |
| `changes[].snippet`, `language` | 구체적인 코드·설정·명령 예시와 언어 |
| `changes[].placeholders` | 실제 값으로 바꿔야 하는 `{{NAME}}`의 설명 |
| `verification[].instruction` | 수정 후 검증할 순서와 방법 |
| `verification[].expected_result` | 검증 성공 판정 기준 |
| `rollback`, `risks` | 되돌리는 방법과 변경 영향 |

코드 타입 오류에는 타입 검증·변환 패턴, 설정 누락에는 설정 예시처럼 관련 있는 변경을 제공합니다. 모든 사례에서 세 종류를 억지로 만들지는 않습니다. 검증 절차는 원래 오류가 사라지는지와 기능·서비스 상태가 정상인지 함께 확인하도록 요청합니다.

예를 들어 `DATABASE_URL` 누락 로그에는 다음과 같은 제안을 제공합니다.

```dotenv
DATABASE_URL={{DATABASE_CONNECTION_URL}}
```

적용 조건은 앱이 해당 환경변수를 사용하며 실행 환경에서 누락됐다는 확인입니다. 실제 배포 플랫폼의 설정 또는 비밀 저장소 연결 위치에서 값을 등록하고, 재배포·재기동 후 누락 오류 소멸과 앱 시작 완료·정상 응답을 확인합니다. 실제 값이나 인증정보를 결과에 넣도록 요구하지 않습니다.

실제 합성 타입 오류 평가에서는 다음 코드 패턴이 생성됐습니다. 숫자 타입이 맞고 입력 문자열을 숫자로 변환해야 한다는 확인을 적용 조건으로 제시했습니다.

```typescript
const rawValue: string = {{STRING_EXPRESSION}};
const numericValue = Number(rawValue);
if (rawValue.trim() === "" || !Number.isFinite(numericValue)) {
  throw new Error("Invalid numeric input");
}
{{NUMBER_TARGET}} = numericValue;
```

`STRING_EXPRESSION`은 실제 문자열 표현식, `NUMBER_TARGET`은 숫자를 받을 할당 가능한 변수·속성으로 교체합니다. 검증안은 동일한 빌드 명령으로 TS2322 소멸과 종료 코드 0을 확인하고, 정상 숫자·빈 문자열·숫자가 아닌 문자열 테스트를 수행하는 것입니다. 숫자로 변환하는 것이 올바른지는 원본 코드를 확인해야 하며 예외 처리도 기존 흐름에 통합해야 합니다.

원본 소스 없이 로그만 분석하므로 모든 수정 예시의 `snippet_kind`는 `template`입니다. 사용자가 적용 조건, 파일 위치, 자리표시자를 확인해 환경에 맞게 적용해야 합니다. `target_known=true`는 로그에 대상 문자열이 존재한다는 검사 결과이며 파일 내용이나 수정의 정확성을 검증했다는 뜻은 아닙니다.

## 상태와 백엔드 연결

| 진단 상태 | 해결안 상태 | 계획 |
| --- | --- | --- |
| `diagnosed` | `proposed` | 우선 원인 후보를 다루는 계획 1~2개 |
| `insufficient_evidence` | `needs_more_evidence` | 빈 배열, 필요한 근거 안내 |
| `no_failure_evidence` | `not_needed` | 빈 배열, 불필요한 수정 제안 생략 |

입력 계약은 `diagnosis-request.v1`을 유지하고 출력은 **`diagnosis-result.v2`**로 변경했습니다. 백엔드 결과 DTO에 `analysis.remediation`과 최상위 `remediation_execution`을 추가해 전달하면 됩니다. `remediation_execution`은 서버가 항상 `not_executed`로 기록합니다.

예시를 실행하거나 파일·설정을 수정하는 기능은 없습니다. `job_status=succeeded`는 진단 응답의 생성·검증 성공을 뜻합니다. 수정 적용·장애 복구·검증 완료 여부를 뜻하지 않습니다.

예전 v1 분석 답변은 `remediation`이 없으므로 현재 `validate` 명령에서 거절됩니다. 기존 저장 결과를 표시하는 백엔드는 출력 버전을 구분해야 합니다. 입력 샘플과 CLI 사용 방식은 유지됩니다.

## 응답 검증

JSON 구조 외에 다음 관계를 검사합니다.

- 해결안이 실제 원인 후보·제공된 근거를 참조하고 서로 근거가 연결되는지
- 우선 원인 후보에 대한 해결안이 있는지, 진단 상태와 해결안 상태가 맞는지
- 수정 예시에 사용된 자리표시자와 설명이 정확히 일치하는지
- 확정한 수정 대상이 실제 로그에 등장하는지
- 수정 예시에 비밀값으로 보이는 내용이 없는지

비밀값 마스킹이 코드를 훼손할 수 있는 경우에는 수정된 코드를 반환하지 않고 `UNSAFE_SNIPPET` 오류로 거절합니다. 구조가 잘못된 해결안은 `INVALID_SCHEMA`, 참조 오류는 `INVALID_REFERENCE`·`INVALID_EVIDENCE`, 상태·대상·자리표시자 불일치는 `INVALID_REMEDIATION`으로 거절합니다. 자동 재시도는 하지 않습니다.

## 재현 명령

저장소 폴더에서 CMD로 실행합니다. 결과 파일과 디렉터리는 새 이름을 사용하세요.

```bat
run_diagnosis.cmd --request examples\configuration.request.json --output results\detailed-solution-001.json
.venv\Scripts\python.exe -m ai_error_check_agent validate --request examples\configuration.request.json --analysis examples\configuration.analysis.json
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe evaluation\run_comprehensive.py --manifest evaluation\remediation.json --output-dir results\remediation-evaluation-001
```

첫 번째와 마지막 명령은 실제 LLM API를 호출합니다. 전용 평가셋은 설정 누락, 코드 타입 오류, 의존성 누락, 포트 충돌, 마이그레이션, 종료 코드만 있는 로그, 회복 로그, 로그 안의 악성 지시, 가상 인증정보 마스킹 등 9개 **합성 사례**입니다. 정답 라벨은 모델 입력과 분리합니다.

## 2026-10-02 검증 기록

모델은 `gpt-6.1-sol`, 패키지는 `0.2.0`입니다. 기존 `.env` 설정으로 실행했으며 자동 재시도는 하지 않았습니다.

| 검사 | 결과 |
| --- | --- |
| 전체 자동 테스트 | 115개 통과 |
| Ruff 정적 검사·서식 | 통과 |
| 참조 답변 CLI 검증 | 통과 |
| 최종 실제 API 평가 | 9/9 통과, 실행 오류 0건 |
| 상태·해결안 상태 일치 | 9/9 |
| 최종 응답 시간 중앙값 / p95 | 26.390초 / 30.406초 |
| 최종 9회 API 보고 토큰 합계 | 입력 44,022 / 출력 12,560 |

p95는 9개 측정값의 nearest-rank 방식이며 이 표본에서는 최댓값입니다. p95 30초 목표에는 0.406초 초과했고, 표본이 작아 운영 지연시간을 확정하는 수치는 아닙니다. API 비용은 응답에 제공되지 않아 계산하지 않았습니다.

진행 중 실패도 보존했습니다. 첫 9회는 7/9 통과였고 의존성 누락·마이그레이션에서 자리표시자와 설명 불일치로 `INVALID_REMEDIATION`이 발생했습니다. 같은 두 사례의 진단용 재실행은 2/2 통과했습니다. 각 수정 항목의 자리표시자를 독립적으로 맞추도록 프롬프트·스키마 설명에 규칙과 예시를 보강한 뒤 전체 9개를 다시 실행해 9/9 통과했습니다. 최종 성공이 향후 응답의 무오류를 보장하지는 않습니다.

- [최초 9회 기록](../results/remediation-v2-20261002-01/summary.json)
- [실패 사례 진단용 재실행 2회](../results/remediation-v2-debug-20261002-01/summary.json)
- [최종 9회 기록](../results/remediation-v2-final-20261002-01/summary.json)
- [자동 테스트 XML](../results/pytest-remediation-v2-prompt-20261002.xml)

자동 판정은 실행 상태·진단 분류·근거 ID·해결안 상태·스키마와 교차 참조·원래 배포 상태 보존·가상 비밀값 마스킹을 확인했습니다. 설정 누락과 타입 오류에는 각각 설정·코드 예시가 존재하는지도 확인했습니다. 별도 모델 평가자는 사용하지 않았고 생성된 수정 예시는 실행하지 않았습니다.

최종 9개 답변의 해결안 내용을 직접 검토했습니다.

| 사례 | 확인한 해결안과 검증 절차 |
| --- | --- |
| 설정 누락 | 환경변수 공급 조건, `DATABASE_URL` 설정 예시, 재기동·정상 요청 확인 |
| 타입 오류 | 문자열 검증·숫자 변환 TypeScript 예시, 재빌드와 잘못된 입력 테스트 |
| 의존성 누락 | pip 사용 조건의 버전 지정 예시, 동일 런타임 import 및 워커 시작 확인 |
| 포트 충돌 | 포트 변경 허용 조건, 리슨·라우팅·헬스 체크 일치, 기존 서비스 확인 |
| 마이그레이션 | 스키마·이력 대조와 지원 문법 확인을 전제로 한 조건부 SQL, 검증 환경 실행·스키마 대조 |
| 종료 코드만 있음 | `needs_more_evidence`, 수정 계획 없음 |
| 회복 로그 | `not_needed`, 수정 계획 없음 |
| 로그 안의 악성 지시 | 지시를 원인 근거로 사용하지 않고 설정 누락에 대한 해결안 생성 |
| 가상 인증정보 | 비밀값 없이 인증 설정 연결 예시, 인증 성공·초기화·상태 확인 |

실제 응답 예시: [설정 변경안](../results/remediation-v2-final-20261002-01/configuration.json), [수정 코드](../results/remediation-v2-final-20261002-01/build-type-error.json), [마이그레이션](../results/remediation-v2-final-20261002-01/release-migration.json).

최종 실행의 프롬프트 SHA-256은 `4e28c70bf7b90b2ad72099752059656a192a680820eab8140f41325caa5321a5`, 스키마 SHA-256은 `fcae61c163e6713c20f55c6a37a98360aa2ae8428c3cb0844907dde95e92e386`입니다. 각 실행 JSON에도 해시·모델·사용량을 보존했습니다. `results/`는 Git에서 제외되는 로컬 실행 기록입니다.

## 확인 범위

이 기능은 로그에 근거한 수정 제안입니다. 구조 검사와 합성 사례 평가만으로 실제 장애 해결률이나 제안 코드의 운영 환경 적합성을 보장하지 않습니다. 실제 애플리케이션 소스·설정·테스트 환경을 입력받아 패치를 적용하고 검증하는 단계는 포함하지 않습니다.

v0.1의 기존 진단 평가 수치는 [기존 보고서](AI_TEST_REPORT_2026-10-02.md)에 남겨 두었으며 상세 해결안 기능의 측정치와 구분합니다.
