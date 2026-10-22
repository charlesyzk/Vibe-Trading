"""Per-endpoint model capabilities used by settings and the agent runtime.

OpenAI-compatible ``/models`` endpoints usually return model IDs, not a
portable capability description. Profiles therefore live in user config and
are keyed by provider, normalized base URL, and exact model ID. The runtime
does not infer image, reasoning, or tool support from a model's name.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config.paths import get_runtime_root

InputModality = Literal["text", "image"]
ReasoningEffort = Literal["low", "medium", "high", "max"]


class ModelCapabilityProfile(BaseModel):
    """Explicit, validated capabilities for one exact provider model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=2048)
    model_name: str = Field(min_length=1, max_length=256)
    context_window_tokens: int = Field(ge=1024, le=10_000_000)
    supports_tool_calling: bool
    supports_reasoning: bool
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list)
    input_modalities: list[InputModality] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capability_consistency(self) -> "ModelCapabilityProfile":
        if "text" not in self.input_modalities:
            raise ValueError("input_modalities must include text")
        if len(set(self.input_modalities)) != len(self.input_modalities):
            raise ValueError("input_modalities cannot contain duplicates")
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise ValueError("reasoning_efforts cannot contain duplicates")
        if self.supports_reasoning and not self.reasoning_efforts:
            raise ValueError("reasoning_efforts is required when reasoning is supported")
        if not self.supports_reasoning and self.reasoning_efforts:
            raise ValueError("reasoning_efforts must be empty when reasoning is unsupported")
        return self


class ModelProfileStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    profiles: list[ModelCapabilityProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_profiles(self) -> "ModelProfileStore":
        keys = [
            _key(profile.provider, profile.base_url, profile.model_name)
            for profile in self.profiles
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("model capability profiles must have unique provider/endpoint/model keys")
        return self


def _profile_path() -> Path:
    return get_runtime_root() / "model_profiles.json"


def normalize_base_url(value: str) -> str:
    """Normalize an endpoint identity without changing its path semantics."""
    value = value.strip()
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if parsed.port:
        host = f"{hostname}:{parsed.port}"
    else:
        host = hostname
    if parsed.username or parsed.password or not scheme or not host:
        raise ValueError("Model profile base URL must be a credential-free HTTP(S) URL")
    if scheme not in {"http", "https"}:
        raise ValueError("Model profile base URL must use HTTP or HTTPS")
    if parsed.query or parsed.fragment:
        raise ValueError("Model profile base URL cannot include query parameters or fragments")
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _key(provider: str, base_url: str, model_name: str) -> tuple[str, str, str]:
    return (
        provider.strip().lower(),
        normalize_base_url(base_url),
        model_name.strip(),
    )


def list_model_profiles() -> list[ModelCapabilityProfile]:
    """Read the complete user profile catalog; malformed data fails loudly."""
    path = _profile_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ModelProfileStore.model_validate(payload).profiles
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid model capability catalog at {path}") from exc


def get_model_profile(
    provider: str,
    base_url: str,
    model_name: str,
) -> ModelCapabilityProfile | None:
    """Return the exact configured profile, never a name-based guess."""
    profiles = list_model_profiles()
    if not profiles:
        return None
    try:
        wanted = _key(provider, base_url, model_name)
    except ValueError:
        return None
    return next(
        (
            profile
            for profile in profiles
            if _key(profile.provider, profile.base_url, profile.model_name) == wanted
        ),
        None,
    )


def upsert_model_profile(profile: ModelCapabilityProfile) -> list[ModelCapabilityProfile]:
    """Atomically save one profile while preserving other provider models."""
    profiles = list_model_profiles()
    wanted = _key(profile.provider, profile.base_url, profile.model_name)
    next_profiles = [
        existing
        for existing in profiles
        if _key(existing.provider, existing.base_url, existing.model_name) != wanted
    ]
    next_profiles.append(profile.model_copy(update={"base_url": normalize_base_url(profile.base_url)}))
    next_profiles.sort(key=lambda item: (item.provider, item.base_url, item.model_name.casefold()))

    path = _profile_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".model-profiles.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                ModelProfileStore(profiles=next_profiles).model_dump(mode="json"),
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return next_profiles


def model_profile_matches(
    profile: ModelCapabilityProfile | None,
    *,
    provider: str,
    base_url: str,
    model_name: str,
) -> bool:
    """Check whether a profile belongs to the active runtime selection."""
    if profile is None:
        return False
    return _key(profile.provider, profile.base_url, profile.model_name) == _key(
        provider, base_url, model_name
    )


def profile_from_payload(payload: Any) -> ModelCapabilityProfile:
    """Validate untrusted API input as a fully specified model profile."""
    profile = ModelCapabilityProfile.model_validate(payload)
    return profile.model_copy(update={"base_url": normalize_base_url(profile.base_url)})
