# 관계 추론 기능 사용과 제한

관계 추론은 마스킹된 로그와 선택된 소스에서 사실을 추출하고, 고정 SPARQL 규칙이 계산한 관계를 기존 LLM 호출에 전달한다. 백엔드 요청과 `diagnosis-result.v3` 응답 규격은 유지한다. 로그 분석 1회와 조건부 소스 분석 1회의 기존 흐름을 사용하며 자동 수정은 실행하지 않는다.

## 실행 설정

서버가 읽는 `.env` 또는 환경변수에 다음을 설정한다. 기본값은 `off`다.

```dotenv
AGENT_REASONING_MODE=assist
AGENT_REASONING_TIMEOUT_MS=200
# 추론 기록을 파일로 남기는 경우
AGENT_KG_MODE=shadow
AGENT_KG_OUTPUT_DIR=.runtime/knowledge
```

기존 서버 실행 명령을 그대로 사용한다. Python에서 앱을 만드는 호출자는 `create_app(..., reasoning_settings=ReasoningSettings(mode="assist"))`로 설정한다. 공개 요청 본문에는 설정을 추가하지 않는다. `AGENT_KG_MODE`는 기존처럼 `off|shadow`이며 추론 사용 여부와 독립적이다.

추론 프로세스는 앱 시작 시 비동기로 예열한다. 준비 전·처리 중·시간 초과·한도 초과·내부 오류에는 기존 프롬프트와 자료로 진단한다. HTTP 요청은 예열 완료를 기다리지 않는다. 정확도 평가 도구는 측정 전에 `wait_ready()`를 명시적으로 기다려 초기 생략을 배제한다.

## 지원하는 사실과 로그 표현

첫 버전은 아래처럼 역할과 대상이 명시된 이벤트 및 일부 리스닝 표현을 처리한다. 이는 기존 로그의 `text`를 해석하는 규칙이며 새 백엔드 필드가 아니다. 다른 형식의 로그도 기존 LLM 진단에는 그대로 전달되지만 규칙 추출 대상은 아닐 수 있다.

```text
listener process_instance=web-1 port=3000
Listening on http://0.0.0.0:3000 process_instance=web-1
probe process_instance=web-1 port=8080 route=direct result=connection_refused
listener_snapshot process_instance=web-1 only_port=3000 complete=true
operation process_instance=web-1 operation=db-connect target=database result=failed
operation process_instance=web-1 operation=db-connect target=database result=succeeded
config_contract process_instance=web-1 role=database key=DATABASE_URL path=src/config.py
config_error process_instance=web-1 role=database key=DATABASE_URl path=src/config.py
```

`healthcheck`는 `probe`, `task`는 `operation`의 별칭이다. 작업 결과에는 `failed|failure|succeeded|success`를 지원한다. `container_id`도 명시적인 실행 식별자로 사용할 수 있다. PID나 `sourceId`만으로 동일 실행을 추측하지 않는다. 실행 식별자는 해당 실행 세대를 구분해야 하며 재시작 후 재사용된 이름만으로는 정확한 연결을 보장할 수 없다.

순서는 동일 source·stage·stream의 `sequence`와 이벤트 내부 줄 번호만 사용한다. 시각이 가깝다는 이유로 서로 다른 출처의 사건을 연결하지 않는다. 로그가 다른 `attempt`를 명시하면 사실 추출에서 제외한다. 입력 자체가 잘못된 배포 시도의 로그를 제공하고 이를 구분할 정보가 없다면 감지할 수 없다.

포트 원인 후보에는 직접 연결 실패뿐 아니라 **같은 이벤트의 명시적인 완전한 리스너 스냅샷**이 필요하다. 따라서 예제의 probe와 snapshot은 하나의 백엔드 로그 이벤트의 여러 줄로 제공되어야 해당 조건을 만족한다. 이런 스냅샷이 없으면 포트 차이 관찰만 제공하고 직접 연결·활성 리스너·매핑을 미확인 사항으로 남긴다. 로그 목록의 `isComplete`를 리스너 목록 완전성으로 대체하지 않는다.

## 코드 사실 추출

선택된 Python 코드에서 `import os`와 리터럴 키를 사용하는 `os.environ["KEY"]` 참조를 분석한다. 선택 범위가 1번 줄부터 연속적이고 Python 구문으로 파싱 가능해야 한다. 코드를 실행하지 않는다.

주석, 동적 키, `.get()` 기본값, 부분 코드 조각, `os` 재정의, 환경변수 별칭을 만드는 쓰기·변경 호출은 원인 후보의 직접 참조 근거로 사용하지 않는다. 다른 언어는 기존 LLM 분석을 사용한다. 소스는 기존과 같이 caller-supplied 커밋 스냅샷이며 실제 실행 코드와의 일치는 별도 확인 사항이다.

## 규칙과 결과

| 규칙 | 결과 | 제한 |
|---|---|---|
| PORT_DIFFERENCE_V1 | 관찰된 리스너와 probe 포트 차이 | 그 자체로 원인 아님 |
| DIRECT_PORT_FAILURE_V1 | 명시적 조건을 만족한 포트 불일치 후보 | 실제 배포 네트워크와 관찰의 일치 확인 필요 |
| SAME_OPERATION_RECOVERY_V1 | 같은 작업의 실패 뒤 성공 관계 | 전체 서비스 정상 판정 아님. 이후 재실패는 반대 근거에 포함 |
| CONFIG_REFERENCE_V1 | 명시된 설정 계약과 코드 참조 키 차이 | 역할·별칭·실행 코드 일치 확인 필요 |
| CONFIG_FAILURE_V1 | 계약·코드·같은 역할의 실행 오류를 연결한 설정 불일치 후보 | 실제 설정과 커밋 한계 유지 |

규칙은 두 층으로 실행한다. 첫 층은 관찰 관계, 두 번째 층은 관계와 오류의 연결이다. LLM의 진단 가설은 사실 입력으로 되돌려 사용하지 않는다. 모델이 작성한 쿼리, 외부 SPARQL 서비스, 그래프 네트워크 로딩은 실행하지 않는다. SHACL은 자료형·출처·관계 구조를 검사하며 실제 원인의 정확성을 보증하지 않는다.

LLM은 기존 원문과 함께 `reasoning_context`를 받는다. 사실 목록과 최대 세 후보에 EV·SC, 반대 근거, 미확인 정보를 포함한다. 최대 4KiB와 기존 전체 프롬프트 제한을 모두 지킨다. 공간이 부족하면 후보 전체 또는 추론 문맥을 생략하며 기존 원문을 삭제하지 않는다. 추론 off에서는 기존 프롬프트·입력·스키마가 동일하다.

## 한도와 저장

API 프로세스당 추론 worker 1개를 사용하고 대기열은 두지 않는다. 단계당 기본 200ms, 입력 256KiB, 사실 100개·2,000트리플, 추론 노드 100개, 전체 5,000트리플을 상한으로 둔다. 추론 트리플에는 더 이른 1,000트리플 제한도 적용한다. 한도 초과는 일부 결과 사용 대신 해당 단계 전체를 생략한다.

100개 작업 후 또는 시간 초과·취소·프로세스 오류 때 worker를 교체한다. 재시작은 요청의 종료 대기에 포함하지 않는다. 저장용 shadow worker는 별도이므로 두 기능을 함께 켜면 두 프로세스의 메모리가 필요하다. 저장·추론 프로세스 모두 정상 앱 종료 때 정리한다.

shadow 저장 디렉터리에 기존 산출물과 함께 아래 파일을 저장한다. 기존 총 산출물 4MiB·입력 레코드 256KiB 한도를 공유하며 파일 권한도 유지한다.

- `reasoning-facts.json.gz`: 단계별 추출 사실과 원문 위치.
- `reasoning-trace.json.gz`: 파생 관계, 규칙·온톨로지·shapes 해시, 실제 적용 문맥, 입력 해시와 한계.
- `reasoning-metrics.json`: 단계별 처리 상태와 시간.

`recorded`가 저장 완료 상태다. 프로세스 강제 종료 시 미완료 비동기 기록의 영속성은 보장하지 않는다. 추론 자료 저장에 실패해도 공개 진단 결과는 바뀌지 않는다.

## 평가 도구

```bash
PYTHONPATH=src .venv/bin/python evaluation/run_relation_ablation.py --output-dir results/relation-offline-new
PYTHONPATH=src .venv/bin/python evaluation/benchmark_reasoning.py --output results/reasoning-benchmark-new.json
```

개발용 24건과 시험용 60건을 제공한다. 사례는 작성한 합성 자료이며 운영 장애 데이터가 아니다. 지원 문법과 규칙군을 공유하므로 외부 독립 벤치마크로 해석하지 않는다. 시험용은 장애 24건, 반례 12건, 정상·복구 12건, 정보 부족 12건이다. 정답 관계는 예측 그래프에서 복사하지 않고 사례 생성 시 미리 정의한다.

아래 명령은 실제 모델 비용이 발생한다. 기본은 한 사례의 세 비교군을 병렬 실행한다. `--case-concurrency 2`는 최대 두 사례·여섯 요청을 동시에 처리한다. 각 작업은 별도 앱·추론 worker를 사용하여 비교 도중 슬롯 부족으로 추론이 빠지지 않도록 한다. 운영 서버의 동시 부하 성능은 별도 벤치마크로 측정한다.

```bash
PYTHONPATH=src .venv/bin/python evaluation/run_relation_ablation.py --live --env-file .env --output-dir results/relation-live-new
```

비교군은 기존 방식, 사실 제공, 규칙 추론이다. 사실 제공군은 SPARQL 파생 규칙을 실행하지 않는다. 동일한 백엔드 입력·아카이브·모델 설정을 사용하지만 LLM의 소스 선택은 달라질 수 있다. 각 호출의 실제 프롬프트·자료·스키마·구조화 응답과 진단 결과를 저장한다. 인증·한도·모델 설정·연결·시간 초과 오류가 나면 이미 진행 중인 호출을 기록한 뒤 다음 묶음을 중단하며 유리한 결과를 골라 재실행하지 않는다.

오프라인 관계 점수는 지정한 파생 관계와 EV·SC 연결의 정확성이다. 전체 온톨로지의 모든 엣지 정확도나 LLM 진단 정확도가 아니다. 실제 LLM 원인 점수는 사전 정규식·분류·근거 기준으로 계산하므로 자유 서술의 의미 정확도에는 사람 검토가 추가로 필요하다.

구현 검증, 자원 측정과 실제 모델 비교의 결과는 [검증 보고서](RELATION_REASONING_TEST_REPORT_2026-10-03.md)를 참고하세요.
