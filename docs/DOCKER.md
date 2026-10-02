# 단일 Docker 이미지 실행

Python API와 OpenCode 1.18.34를 같은 Linux 이미지에 포함한다. 기본 실행 방식은 OpenCode이며, 같은 이미지에서 OpenAI·Sakana API를 직접 호출하는 방식도 선택할 수 있다. 현재 애플리케이션 버전은 `0.5.0`이다. Kubernetes 배포 파일이나 AWS 리소스 생성은 포함하지 않는다.

## 빌드

저장소 루트에서 Docker의 Linux 컨테이너 엔진을 사용한다.

```bat
docker build -t iris-error-check-agent:0.5.0 -t iris-error-check-agent:latest .
```

다른 아키텍처를 사용할 때는 EKS 노드와 플랫폼을 맞춰 빌드한다. 예를 들어 amd64 이미지는 `docker build --platform linux/amd64 -t iris-error-check-agent:0.5.0 .`으로 만든다. 실제 빌드·실행 검증 범위는 [검증 기록](DOCKER_TEST_REPORT_2026-10-02.md)에 기록한다.

`.dockerignore`는 빌드에 필요한 소스·의존성·패키지 메타데이터만 허용한다. `.env`, `.git`, `.venv`, `.runtime`, 테스트 결과와 로컬 키 파일은 빌드 컨텍스트에서 제외한다. Python 실행 의존성은 `requirements.lock`에 고정했고, npm과 Python 패키지 빌드는 별도 단계에서 수행한다. 최종 이미지에는 OpenCode 실행 바이너리와 Python 실행 환경, Git, 인증서, tini가 들어간다.

## 실행

기존 `.env`에 `LLM_API_KEY`, `LLM_MODEL` 및 `AGENT_API_KEY`를 설정한다. `AGENT_API_KEY`는 백엔드가 에이전트에 보내는 별도의 인증 키로, 공백 없는 ASCII 32자 이상이다. Docker `--env-file`용 값은 따옴표 없이 작성한다.

```dotenv
LLM_API_KEY=YOUR_LLM_API_KEY
LLM_PROVIDER=openai
LLM_MODEL=gpt-6.1-sol
LLM_BASE_URL=https://api.openai.com/v1
AGENT_API_KEY=REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_32_ASCII_CHARACTERS
```

로컬 CMD에서 다음 명령으로 실행한다. 이미지는 서버 모드로 실행하며 API 키가 없으면 시작하지 않는다. `--dev`는 컨테이너 외부 연결용 설정으로 사용하지 않는다.

```bat
docker run --rm --name iris-error-check-agent --env-file .env -e AGENT_RUNTIME=opencode -p 127.0.0.1:8001:8001 --stop-timeout 280 iris-error-check-agent:0.5.0
```

`.env`는 이미지에 복사되지 않고 실행 시 환경변수로 주입된다. `-e AGENT_RUNTIME=opencode`는 `.env`에 `AGENT_RUNTIME=direct`가 있어도 OpenCode 모드를 선택한다. 직접 호출하려면 이 값을 `direct`로 바꾼다.

다른 CMD에서 상태와 진단 요청을 확인한다.

```bat
curl.exe http://127.0.0.1:8001/healthz
curl.exe -X POST http://127.0.0.1:8001/diagnose -H "Content-Type: application/json" -H "X-API-Key: YOUR_AGENT_API_KEY" --data-binary "@examples/backend-log-only.request.json"
docker stop --time 280 iris-error-check-agent
```

`YOUR_AGENT_API_KEY`는 같은 `.env`에 넣은 에이전트 인증 키로 교체한다. 진단 요청은 실제 LLM 사용량을 발생시킨다. 동일 Docker 네트워크의 백엔드는 컨테이너 이름과 8001 포트로 호출할 수 있다.

## 컨테이너 구성

- API는 `0.0.0.0:8001`, 내부 OpenCode는 `127.0.0.1:4096`에서 실행한다. 이미지에서는 8001만 노출한다.
- 일반 사용자 UID/GID 10001로 실행한다. 임시 실행 상태는 `/app/.runtime/opencode-runs`, 압축 처리 임시 파일은 `/tmp`를 사용한다.
- `tini`가 PID 1을 맡고 SIGTERM을 API에 전달한다. API의 ASGI lifespan 종료에서 OpenCode 프로세스를 정리한다. Uvicorn이 종료 후 반환하는 SIGTERM 코드 143은 `tini -e 143`으로 0에 매핑한다. 강제 종료 코드 137과 그 외 오류는 그대로 유지한다.
- `/healthz`를 30초마다 확인하며 모델은 호출하지 않는다. 이 검사는 API 생존 확인이며, LLM 제공자나 S3의 정상 동작을 보장하지 않는다.
- 이미지 안에서는 모델·SDK·npm 패키지를 추가 설치하지 않고 설치된 OpenCode 버전을 확인한다. 처음 컨테이너를 만들 때 상태 폴더는 비어 있다.

OpenCode 경로의 단계 제한시간은 GPT 기본 60초, Sakana 기본 120초이며 소스 분석은 최대 두 단계와 다운로드 시간을 사용한다. 실행 예시는 두 단계와 다운로드·정리를 기다릴 수 있도록 280초의 종료 유예를 준다. 직접 호출 모드 등에서 제한시간을 높이면 종료 유예와 백엔드·프록시 요청 제한시간도 함께 맞춘다.

Sakana 모델도 쓰려면 실행 시 Secret 환경변수로 `SAKANA_API_KEY`를 추가한다. 키를 추가하거나 변경한 뒤 컨테이너를 재시작하면 되며, 이미지 재빌드는 필요하지 않다. 기본 GPT 설정과 함께 주입하면 두 제공자의 모델을 요청별로 선택할 수 있다. `GET /models`와 `POST /diagnose?model=sakana/fugu`는 같은 8001 포트에서 제공한다. 키가 없는 Sakana 모델을 선택하면 기본 모델로 바꾸지 않고 HTTP 503을 반환한다. [모델 선택과 EKS 설정](MODEL_SELECTION.md)을 참고한다.

EKS에서는 이미지의 Docker HEALTHCHECK와 별도로 Kubernetes probe를 설정해야 한다. 읽기 전용 루트 파일시스템을 적용할 경우 `/app/.runtime/opencode-runs`, `/tmp`, `/home/agent`의 필요한 쓰기 경로를 볼륨으로 제공하고 권한을 맞춘다. S3와 외부 LLM API에 대한 아웃바운드 HTTPS 연결도 필요하다.
