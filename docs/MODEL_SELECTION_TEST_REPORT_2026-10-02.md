# 모델 선택 검증 — 2026-10-02

## 결과

- 자동 테스트: **259개 통과**. 기존 245개에 모델 목록·선택·키 분리·동시 요청 테스트를 추가했다.
- Ruff: 통과.
- Windows OpenCode 1.18.34: 실제 TUI에서 `/models`를 열고 `fugu-max`를 선택하여 `Iris_diagnosis · fugu-max Sakana AI` 표시를 확인했다. 선택만 했으며 이 화면에서 모델 추론은 호출하지 않았다. `/exit` 종료 코드 0을 확인했다.
- 실제 GPT API: `model=openai/gpt-6.1-sol`로 지정한 백엔드 JSON 진단 1건, HTTP 200 및 `diagnosed`. 로그 단계 29.7초, OpenCode 세션 삭제 확인. 이 호출은 버전 표기를 0.5.0으로 갱신하기 전에 수행해 저장된 실행 메타데이터의 service_version은 0.4.2다.
- 실제 OpenCode + 로컬 Sakana 프로토콜 테스트 서버: `fugu`, `fugu-max`, `fugu-ultra`, `sakana-namazu` 4종 모두 `/v1/responses`로 선택 모델과 Sakana 전용 테스트 키를 전송하고 SSE 응답을 읽었다. 각 요청의 `reasoning.effort=high` 확인.
- 최종 네이티브 모델 목록: Sakana 모델의 추론 수준에는 `high`, `xhigh`, `max`만 남고 지원하지 않는 `low`, `medium` 등은 제거됨을 확인했다.
- 최종 Docker `iris-error-check-agent:0.5.0`, `linux/amd64`: 빌드·기동·인증·모델 목록·HEALTHCHECK·SIGTERM 종료 코드 0 통과.

실제 Sakana API 키가 없어 Sakana 서비스의 인증, 계정별 모델 접근 권한, 실제 추론 결과와 진단 품질은 검증하지 않았다. API 키 등록 뒤 합성 로그와 소스 시나리오로 추가 확인해야 한다. OpenCode TUI는 대화형 진단이며 서비스 API의 자동 마스킹과 JSON 근거 검증을 수행하지 않는다.

## 자동 검증 항목

1. 같은 공통 모델 목록을 OpenCode와 서비스 API에서 사용한다.
2. `/models`에 인증이 필요하며 실제 키는 응답·생성 설정 문자열에 포함되지 않는다.
3. 키 없는 Sakana 모델은 `available=false`, 호출 시 503이다. OpenAI 키를 대신 보내지 않는다.
4. 알 수 없는 모델과 빈 모델 선택은 422이며 모델 호출이 발생하지 않는다.
5. OpenAI/Sakana 동시 요청이 겹쳐도 각각의 로그·소스 두 단계가 같은 선택 모델을 사용한다.
6. 기존 백엔드 전체 JSON, data 객체 및 소스 파일을 포함한 기존 요청 형식을 유지한다.
7. 제공자와 공식 URL의 불일치, 잘못된 키·모델 목록·추론 수준·출력 제한은 거절한다.
8. 직접 Responses 경로에서도 Sakana 전용 키를 쓰고 제공자와 검증된 모델 별칭을 기록한다.

## 로컬 검증 자료

- `results/models-v050-probe/`: 실제 GPT 진단 결과 및 네이티브 OpenCode의 Sakana 프로토콜 요청 요약
- `results/iris-agent-smoke-ea1e7fd2/`: 최종 Linux 이미지 인증·모델 목록·종료 결과
- 최종 Linux 이미지 manifest: `sha256:c36b6c07d70e0880a143e6eced2e307734e39ec56c410f888aa9981b4cff2e04`

결과 폴더는 Git에서 제외한다. `.env`는 변경하지 않았고 테스트용 OpenCode와 Docker 컨테이너는 종료했다. 이미지는 로컬에 남겨 두었다. EKS 배포, 실제 IRIS 프론트·백엔드 연결 및 운영 S3 다운로드는 이번 검증 범위에 포함하지 않았다.

재실행할 자동 검사:

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

사용법과 서비스 연결 규격은 [모델 선택 문서](MODEL_SELECTION.md)를 참고한다.
