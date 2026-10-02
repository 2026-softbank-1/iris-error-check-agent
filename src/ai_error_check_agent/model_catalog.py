"""Shared, server-owned model choices for the API and interactive OpenCode."""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from .direct_api import DirectModelProfile, load_direct_settings
from .errors import DiagnosisError

SAKANA_MODELS = ("fugu", "fugu-max", "fugu-ultra", "sakana-namazu")


def model_id(profile):
    return f"{profile.provider_id}/{profile.model_id}"


@dataclass(frozen=True)
class ModelChoice:
    profile: DirectModelProfile
    key: SecretStr | None = field(repr=False)

    def public(self):
        return {
            "id": model_id(self.profile),
            "provider": self.profile.provider_id,
            "model": self.profile.model_id,
            "available": self.key is not None,
            "unavailableReason": None if self.key else "missing_api_key",
        }


class ModelCatalog:
    def __init__(self, choices, default):
        self.choices = {model_id(c.profile): c for c in choices}
        if len(self.choices) != len(choices) or default not in self.choices:
            raise DiagnosisError("INVALID_CONFIG", "모델 목록과 기본 모델을 확인하세요.")
        self.default = default
        self.select(default)

    def select(self, selection=None):
        choice = self.choices.get(selection if selection is not None else self.default)
        if choice is None:
            raise DiagnosisError("UNKNOWN_MODEL", "서버에 등록되지 않은 모델입니다.")
        if choice.key is None:
            raise DiagnosisError(
                "MODEL_NOT_CONFIGURED", "해당 모델의 서버 API 키가 설정되지 않았습니다."
            )
        return choice

    def public(self):
        return {
            "defaultModel": self.default,
            "agent": "iris_diagnosis",
            "models": [c.public() for c in self.choices.values()],
        }


def load_catalog(env_file: Path, *, default_profile=None, default_key=None, environ=None):
    if env_file.exists() and env_file.stat().st_size > 65_536:
        raise DiagnosisError("INVALID_CONFIG", "설정 파일 크기를 확인하세요.")
    config = {
        **dotenv_values(env_file, encoding="utf-8-sig", interpolate=False),
        **(os.environ if environ is None else environ),
    }
    if default_profile is None:
        default_profile, default_key = load_direct_settings(
            env_file, config.get("AGENT_MODEL_PROFILE_ID") or "profile-demo-a", environ
        )
    choices = [ModelChoice(default_profile, default_key)]
    providers = {
        "openai": (
            str(config.get("OPENAI_API_KEY") or config.get("LLM_API_KEY") or "").strip(),
            config.get("OPENAI_MODELS") or "gpt-6.1-sol",
        ),
        "sakana": (
            str(config.get("SAKANA_API_KEY") or "").strip(),
            config.get("SAKANA_MODELS") or ",".join(SAKANA_MODELS),
        ),
    }
    try:
        for provider, (key, models) in providers.items():
            if key and (not key.isascii() or any(c.isspace() for c in key)):
                raise ValueError("invalid key")
            names = [m.strip() for m in models.split(",")]
            if not 1 <= len(names) <= 12 or len(set(names)) != len(names):
                raise ValueError("invalid model list")
            for name in names:
                identity = f"{provider}/{name}"
                if identity == model_id(default_profile):
                    continue
                prefix = provider.upper()
                profile = DirectModelProfile(
                    profile_id="model-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    provider_id=provider,
                    model_id=name,
                    base_url=f"https://api.{'openai.com' if provider == 'openai' else 'sakana.ai'}/v1",
                    timeout_seconds=float(
                        config.get(f"{prefix}_TIMEOUT_SECONDS")
                        or (60 if provider == "openai" else 120)
                    ),
                    max_output_tokens=int(
                        config.get(f"{prefix}_MAX_OUTPUT_TOKENS")
                        or (4096 if provider == "openai" else 8192)
                    ),
                    reasoning_effort=config.get(f"{prefix}_REASONING_EFFORT")
                    or ("low" if provider == "openai" else "high"),
                )
                choices.append(ModelChoice(profile, SecretStr(key) if key else None))
    except (ValueError, ValidationError):
        raise DiagnosisError(
            "INVALID_CONFIG", "제공자별 모델 목록·키·제한 설정을 확인하세요."
        ) from None
    return ModelCatalog(choices, model_id(default_profile))
