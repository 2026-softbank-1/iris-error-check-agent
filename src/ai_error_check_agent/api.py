"""Standalone HTTP entry point: python -m ai_error_check_agent.api --dev."""

import argparse
import asyncio
import hmac
import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .direct_api import OpenAIResponsesRuntime, load_direct_settings
from .errors import DiagnosisError
from .source_analysis import diagnose_with_source
from .source_contracts import DiagnoseAPIRequest, DiagnoseAPIResult
from .validation import strict_json

MAX_BODY_BYTES = 1_048_576


def error_response(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class RequestGate:
    """Authenticate and bound the body before JSON parsing; never echo input values."""

    def __init__(self, app, api_key, max_body_bytes):
        self.app, self.api_key, self.max_body_bytes = app, api_key, max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/diagnose":
            return await self.app(scope, receive, send)
        if scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        key = headers.get(b"x-api-key", b"")
        if self.api_key and not hmac.compare_digest(key, self.api_key.encode("ascii")):
            return await error_response(401, "UNAUTHORIZED", "유효한 X-API-Key가 필요합니다.")(
                scope, receive, send
            )
        if scope["method"] != "POST":
            return await self.app(scope, receive, send)
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if content_type != b"application/json":
            return await error_response(
                415, "INVALID_CONTENT_TYPE", "application/json을 사용하세요."
            )(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body_bytes:
                        return await error_response(
                            413, "BODY_TOO_LARGE", "요청 본문이 너무 큽니다."
                        )(scope, receive, send)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await error_response(408, "BODY_TIMEOUT", "요청 본문 수신 시간이 초과됐습니다.")(
                scope, receive, send
            )
        try:
            strict_json(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return await error_response(
                422, "INVALID_REQUEST", "올바른 단일 JSON 객체가 필요합니다."
            )(scope, receive, send)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


def create_app(
    runtime_factory,
    *,
    api_key=None,
    dev=False,
    allowed_origins=(),
    max_concurrent=2,
    max_body_bytes=MAX_BODY_BYTES,
):
    if not dev and not api_key:
        raise DiagnosisError("MISSING_AGENT_API_KEY", "서버 모드는 AGENT_API_KEY가 필요합니다.")
    if api_key and (
        not api_key.isascii() or len(api_key) < 32 or any(c.isspace() for c in api_key)
    ):
        raise DiagnosisError(
            "INVALID_AGENT_API_KEY", "AGENT_API_KEY는 공백 없는 ASCII 32자 이상입니다."
        )
    if max_concurrent < 1 or max_body_bytes < 1:
        raise ValueError("limits must be positive")
    app = FastAPI(
        title="IRIS Error Doctor",
        version=__version__,
        description="로그 우선 진단 및 백엔드 제공 소스의 조건부 분석. 수정은 제안만 합니다.",
    )
    app.add_middleware(RequestGate, api_key=api_key, max_body_bytes=max_body_bytes)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(allowed_origins),
            allow_methods=["POST"],
            allow_headers=["Content-Type", "X-API-Key"],
        )
    active = 0
    request_schema = DiagnoseAPIRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    definitions = request_schema.pop("$defs", {})

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.post(
        "/diagnose",
        response_model=DiagnoseAPIResult,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": request_schema}},
            },
            "security": [{"AgentAPIKey": []}] if api_key else [],
        },
        responses={
            code: {"description": description}
            for code, description in {
                401: "API 키 누락 또는 오류",
                408: "본문 수신 시간 초과",
                413: "본문 크기 초과",
                415: "JSON 형식 필요",
                422: "입력 오류",
                429: "동시 요청 한도",
                502: "모델 응답 오류",
                503: "모델 연결·설정·사용 한도 오류",
                504: "로그 진단 시간 초과",
            }.items()
        },
    )
    async def diagnose_endpoint(request: Request):
        nonlocal active
        try:
            payload = DiagnoseAPIRequest.model_validate_json(await request.body())
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            return error_response(
                422, "INVALID_REQUEST", "요청 필드·로그·소스 파일 규격을 확인하세요."
            )
        # All requests in this worker share one event loop; check/increment has no await.
        if active >= max_concurrent:
            return error_response(429, "BUSY", "진단 처리 중입니다. 잠시 후 다시 시도하세요.")
        active += 1
        try:
            result = await diagnose_with_source(payload, runtime_factory)
        except DiagnosisError as exc:
            return error_response(422, exc.code, exc.message)
        finally:
            active -= 1
        if result["job_status"] != "succeeded":
            code = result["error"]["code"]
            status = 504 if code == "MODEL_TIMEOUT" else 502
            if code in {
                "MODEL_AUTH_ERROR",
                "MODEL_RATE_LIMIT",
                "MODEL_NOT_FOUND",
                "MODEL_CONNECTION_ERROR",
            }:
                status = 503
            elif code in {"INPUT_TOO_LARGE", "PROFILE_MISMATCH", "EMPTY_LOGS"}:
                status = 422
            return JSONResponse(result, status_code=status)
        return result

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title, version=app.version, description=app.description, routes=app.routes
            )
            schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions)
            schema["components"]["securitySchemes"] = {
                "AgentAPIKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            }
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="IRIS Error Doctor API")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--dev", action="store_true", help="루프백 전용 API 키 없는 개발 모드")
    args = parser.parse_args(argv)
    if args.dev and args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("--dev는 루프백 주소에서만 사용할 수 있습니다.")
    try:
        if args.env_file.exists() and args.env_file.stat().st_size > 65_536:
            raise DiagnosisError("INVALID_CONFIG", ".env 파일이 너무 큽니다.")
        config = {
            **dotenv_values(args.env_file, encoding="utf-8-sig", interpolate=False),
            **os.environ,
        }
        profile, key = load_direct_settings(
            args.env_file, config.get("AGENT_MODEL_PROFILE_ID") or "profile-demo-a"
        )
        origins = tuple(
            o.strip() for o in (config.get("AGENT_ALLOWED_ORIGINS") or "").split(",") if o.strip()
        )
        if "*" in origins:
            raise DiagnosisError("INVALID_CONFIG", "CORS에는 허용할 프론트 주소를 명시하세요.")
        app = create_app(
            lambda: OpenAIResponsesRuntime(profile, key),
            api_key=config.get("AGENT_API_KEY") or None,
            dev=args.dev,
            allowed_origins=origins,
        )
    except (DiagnosisError, OSError) as exc:
        parser.error(
            exc.message if isinstance(exc, DiagnosisError) else "설정 파일을 읽지 못했습니다."
        )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
