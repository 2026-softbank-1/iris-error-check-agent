import json
from pathlib import Path

import pytest

from ai_error_check_agent.contracts import DiagnosisRequest
from ai_error_check_agent.runtime import ModelProfile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def request_data():
    return json.loads((ROOT / "examples/configuration.request.json").read_text("utf-8"))


@pytest.fixture
def diagnosis_request(request_data):
    return DiagnosisRequest.model_validate_json(json.dumps(request_data))


@pytest.fixture
def analysis_data():
    return json.loads((ROOT / "examples/configuration.analysis.json").read_text("utf-8"))


@pytest.fixture
def profile():
    return ModelProfile(
        profile_id="profile-demo-a",
        base_url="http://127.0.0.1:4096",
        provider_id="test-provider",
        model_id="test-model",
        expected_runtime_version="test-version",
    )
