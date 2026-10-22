"""Contract checks for exact model profiles and chat image preparation."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from src.agent.context_budget import resolve_window
from src.providers.chat import ChatLLM
from src.providers.model_profiles import (
    ModelCapabilityProfile,
    get_model_profile,
    upsert_model_profile,
)


def _profile(*, images: bool = True) -> ModelCapabilityProfile:
    return ModelCapabilityProfile(
        provider="openai",
        base_url="https://example.test/v1",
        model_name="custom-model",
        context_window_tokens=64000,
        supports_tool_calling=True,
        supports_reasoning=False,
        reasoning_efforts=[],
        input_modalities=["text", "image"] if images else ["text"],
    )


def _uploaded_message(image_path, document_path) -> str:
    attachments = [
        {"filename": "chart.png", "path": str(image_path)},
        {"filename": "notes.pdf", "path": str(document_path)},
    ]
    return f"[Uploaded files: {json.dumps(attachments)}]\n\nAnalyze both files"


def test_profile_matches_only_the_exact_endpoint_and_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    upsert_model_profile(_profile())

    assert get_model_profile("OPENAI", "https://EXAMPLE.test/v1/", "custom-model") is not None
    assert get_model_profile("openai", "https://example.test/V1", "custom-model") is None
    assert get_model_profile("openai", "https://example.test/v1", "another-model") is None


def test_profile_window_beats_name_catalog_without_overriding_learned_limit() -> None:
    assert resolve_window("openai", "custom-model", profile_window=64000) == (64000, "profile:exact_model")
    assert resolve_window("openai", "custom-model", profile_window=64000, learned_window=32000) == (
        32000, "learned:context_length_error"
    )


def test_image_request_keeps_non_image_attachments(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"image bytes")
    document_path = tmp_path / "notes.pdf"
    document_path.write_bytes(b"document bytes")
    monkeypatch.setattr("src.providers.chat.allowed_file_roots", lambda: [tmp_path])
    chat = ChatLLM.__new__(ChatLLM)
    chat.model_profile = _profile()
    original = [{"role": "user", "content": _uploaded_message(image_path, document_path)}]

    prepared = chat._request_messages(original)

    assert original[0]["content"].startswith("[Uploaded files:")
    blocks = prepared[0]["content"]
    assert len(blocks) == 2
    assert str(document_path) in blocks[0]["text"]
    assert str(image_path) not in blocks[0]["text"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_profile_uses_local_ocr_without_sending_pixels(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"image bytes")
    document_path = tmp_path / "notes.pdf"
    document_path.write_bytes(b"document bytes")
    monkeypatch.setattr("src.providers.chat.allowed_file_roots", lambda: [tmp_path])
    monkeypatch.setattr(ChatLLM, "_local_image_ocr", staticmethod(lambda path, name: "OCR: price 100"))
    chat = ChatLLM.__new__(ChatLLM)
    chat.model_profile = _profile(images=False)

    prepared = chat._request_messages([
        {"role": "user", "content": _uploaded_message(image_path, document_path)}
    ])

    blocks = prepared[0]["content"]
    assert any("OCR: price 100" in block.get("text", "") for block in blocks)
    assert not any(block.get("type") == "image_url" for block in blocks)


def test_image_without_profile_requires_explicit_configuration(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"image bytes")
    document_path = tmp_path / "notes.pdf"
    monkeypatch.setattr("src.providers.chat.allowed_file_roots", lambda: [tmp_path])
    chat = ChatLLM.__new__(ChatLLM)
    chat.model_profile = None

    with pytest.raises(ValueError, match="explicit capability profile"):
        chat._request_messages([
            {"role": "user", "content": _uploaded_message(image_path, document_path)}
        ])


def test_tiff_is_converted_to_png_for_direct_image_input(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "chart.tiff"
    Image.new("RGB", (2, 2), color="white").save(image_path)
    monkeypatch.setattr("src.providers.chat.allowed_file_roots", lambda: [tmp_path])
    chat = ChatLLM.__new__(ChatLLM)
    chat.model_profile = _profile()
    prompt = f'[Uploaded files: {json.dumps([{"filename": "chart.tiff", "path": str(image_path)}])}]\n\nRead this chart'

    prepared = chat._request_messages([{"role": "user", "content": prompt}])

    assert prepared[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
