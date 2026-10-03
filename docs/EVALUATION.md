# 개발 사례 반복 평가

> 2026-10-04 README 재구성 때 기존 README(v0.5.0 시점)에서 원문 그대로 옮겼다. 현재 운영 상태는 [README](../README.md)를 기준으로 한다.


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

상세 해결안 추가 전인 v0.1의 2026-10-02 실행 결과와 제한은 [AI 테스트 보고서](AI_TEST_REPORT_2026-10-02.md)에 기록했습니다. v0.2의 기능과 검증 결과는 [상세 해결안 안내](REMEDIATION_V2.md)를 참고하세요.

최종 전체 회귀 평가에는 기본 사례·확장 사례·종료 코드 137 단독·예상된 테스트 오류를 포함한 19개 시나리오가 있습니다. 회복 사례를 3회 반복하므로 총 21회 호출합니다.

```bat
.venv\Scripts\python.exe evaluation\run_comprehensive.py --manifest evaluation\full-regression.json --output-dir results\full-regression-001
```

