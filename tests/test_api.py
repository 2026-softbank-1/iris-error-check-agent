import asyncio
import json

import httpx
import pytest
from test_source_analysis import api_input, factory, selection, source_response

from ai_error_check_agent.api import create_app, main
from ai_error_check_agent.errors import DiagnosisError

KEY = "test-agent-key-" + "0" * 32


def send(app, method="POST", path="/diagnose", **kwargs):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            return await c.request(method, path, **kwargs)

    return asyncio.run(call())


def test_api_two_stage_response(request_data, analysis_data):
    create, seen, _ = factory([selection(analysis_data), source_response(analysis_data)])
    response = send(
        create_app(create, api_key=KEY), json=api_input(request_data), headers={"X-API-Key": KEY}
    )
    assert response.status_code == 200, response.text
    assert response.json()["schema_version"] == "diagnosis-result.v3"
    assert response.json()["source_analysis"]["status"] == "analyzed"
    assert len(seen) == 2


def test_api_log_only_compatibility(request_data, analysis_data):
    create, _, _ = factory([selection(analysis_data, False)])
    response = send(create_app(create, dev=True), json={"diagnosis": request_data})
    assert response.status_code == 200
    assert response.json()["source_analysis"]["status"] == "not_needed"


def test_auth_checked_before_model_or_body():
    create, _, runtimes = factory([])
    app = create_app(create, api_key=KEY)
    for key in ("", "wrong"):
        response = send(app, content="invalid", headers={"X-API-Key": key})
        assert response.status_code == 401
    assert not runtimes


def test_all_openapi_refs_resolve():
    schema = create_app(lambda: None, dev=True).openapi()

    def check(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(schema)


def test_server_requires_key_and_dev_forbids_public_host():
    with pytest.raises(DiagnosisError):
        create_app(lambda: None)
    with pytest.raises(DiagnosisError):
        create_app(lambda: None, api_key="short")
    with pytest.raises(SystemExit):
        main(["--dev", "--host", "0.0.0.0"])


@pytest.mark.parametrize(
    "body",
    [
        b'{"diagnosis":{},"diagnosis":{}}',
        b'{"x":NaN}',
        b'{"api_key":"DO_NOT_ECHO_SECRET"}',
        b"\xff",
        b"[]",
    ],
)
def test_invalid_input_does_not_echo_payload(body):
    create, _, runtimes = factory([])
    response = send(
        create_app(create, dev=True), content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "DO_NOT_ECHO_SECRET" not in response.text
    assert not runtimes


def test_oversized_and_wrong_content_type_rejected():
    create, _, runtimes = factory([])
    app = create_app(create, dev=True, max_body_bytes=10)
    assert (
        send(
            app, content='{"long":"12345"}', headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
    assert send(app, content="{}").status_code == 415
    assert not runtimes


def test_schema_and_cors():
    app = create_app(lambda: None, api_key=KEY, allowed_origins=["http://localhost:3000"])
    schema = send(app, "GET", "/openapi.json").json()
    assert "SourceSnapshot" in schema["components"]["schemas"]
    operation = schema["paths"]["/diagnose"]["post"]
    assert operation["security"] == [{"AgentAPIKey": []}]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["required"] == [
        "diagnosis"
    ]
    assert send(app, "GET", "/healthz").status_code == 200
    for origin, status in [("http://localhost:3000", 200), ("https://bad.invalid", 400)]:
        response = send(
            app,
            "OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key, Content-Type",
            },
        )
        assert response.status_code == status


@pytest.mark.parametrize(
    "code,status",
    [
        ("MODEL_TIMEOUT", 504),
        ("MODEL_RATE_LIMIT", 503),
        ("MODEL_ERROR", 502),
        ("INPUT_TOO_LARGE", 422),
    ],
)
def test_model_error_mapping(request_data, code, status):
    create, _, _ = factory([DiagnosisError(code, "safe")])
    response = send(create_app(create, dev=True), json=api_input(request_data))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_concurrent_limit_and_release(monkeypatch, request_data, analysis_data):
    from ai_error_check_agent import api

    original = api.diagnose_with_source

    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()

        async def slow(*args):
            entered.set()
            await resume.wait()
            return await original(*args)

        monkeypatch.setattr(api, "diagnose_with_source", slow)
        create, _, _ = factory([selection(analysis_data, False), selection(analysis_data, False)])
        app = create_app(create, dev=True, max_concurrent=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
            first = asyncio.create_task(c.post("/diagnose", json=api_input(request_data)))
            await entered.wait()
            second = await c.post("/diagnose", json=api_input(request_data))
            assert second.status_code == 429
            resume.set()
            assert (await first).status_code == 200
            assert (await c.post("/diagnose", json=api_input(request_data))).status_code == 200

    asyncio.run(scenario())


def test_invalid_source_is_rejected_before_dispatch(request_data):
    payload = api_input(request_data)
    payload["source_snapshot"]["files"][0]["path"] = ".env"
    create, _, runtimes = factory([])
    response = send(
        create_app(create, dev=True),
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert not runtimes
