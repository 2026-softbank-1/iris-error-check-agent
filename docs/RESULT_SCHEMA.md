# 결과 필드와 상세 해결안

> 2026-10-04 README 재구성 때 기존 README(v0.5.0 시점)에서 원문 그대로 옮겼다. 현재 운영 상태는 [README](../README.md)를 기준으로 한다.

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

