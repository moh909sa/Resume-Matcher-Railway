"""Mobile tailoring defaults and user-facing contract regression tests."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.mobile_tailoring import (
    MobileTailorRequest,
    _preview_length_options,
    preview_mobile_tailor,
)


def test_defaults_are_full_tailoring_unlimited_pages_and_all_bullets():
    request = MobileTailorRequest(
        resume_id=uuid4(), job_description="Senior engineer required. " * 4
    )
    assert request.max_pages is None
    assert request.max_bullets_per_entry is None
    # Crucial: passing neither preference must SKIP both page fitting and truncation.
    assert _preview_length_options(request) == {}
    assert not hasattr(request, "prompt_id")


def test_optional_explicit_length_limits_are_applied_only_when_requested():
    request = MobileTailorRequest(
        resume_id=uuid4(),
        job_description="Senior engineer required. " * 4,
        max_pages=3,
        max_bullets_per_entry=3,
    )
    options = _preview_length_options(request)
    assert options["max_pages"] == 3
    assert options["max_bullets_per_entry"] == 3
    assert options["page_fit"].pageSize == "A4"

    bullet_only = request.model_copy(update={"max_pages": None})
    assert "page_fit" not in _preview_length_options(bullet_only)
    assert "max_pages" not in _preview_length_options(bullet_only)


@pytest.mark.parametrize(
    "options",
    [
        {"max_pages": 1},
        {"max_pages": 4},
        {"max_bullets_per_entry": 2},
        {"max_bullets_per_entry": 6},
    ],
)
def test_unsupported_preference_rejected(options):
    with pytest.raises(ValidationError):
        MobileTailorRequest(
            resume_id=uuid4(),
            job_description="Senior engineer required. " * 4,
            **options,
        )


def test_short_job_description_is_rejected():
    with pytest.raises(ValidationError):
        MobileTailorRequest(resume_id=uuid4(), job_description="Not enough")


@pytest.mark.asyncio
async def test_unowned_cv_rejected_before_any_ai_call():
    user_id = str(uuid4())
    backend = AsyncMock()
    backend.get_record.return_value = None
    body = MobileTailorRequest(
        resume_id=uuid4(), job_description="Senior engineer required. " * 4
    )
    with pytest.raises(HTTPException) as err:
        await preview_mobile_tailor(body, user_id=user_id, backend=backend)
    assert err.value.status_code == 404
    backend.get_record.assert_awaited_once_with(user_id, str(body.resume_id))
    backend.create_tailored_record.assert_not_called()


# Railway model selection is part of the mobile tailoring contract. Keep these
# regressions in the mobile suite so the existing CI job exercises them.
@pytest.mark.parametrize(
    ("mobile", "overrides", "expected"),
    [
        (True, {}, ("openrouter", "saved/model")),
        (
            True,
            {"llm_model": "publisher/replacement"},
            ("openrouter", "publisher/replacement"),
        ),
        (
            True,
            {"llm_model": "  publisher/replacement  "},
            ("openrouter", "publisher/replacement"),
        ),
        (True, {"llm_model": ""}, ("openrouter", "saved/model")),
        (True, {"llm_model": "   "}, ("openrouter", "saved/model")),
        (True, {"llm_provider": "openrouter"}, ("openrouter", "saved/model")),
        (True, {"llm_provider": "openai"}, ("openrouter", "saved/model")),
        (
            True,
            {"llm_provider": "openrouter", "llm_model": "publisher/replacement"},
            ("openrouter", "publisher/replacement"),
        ),
        (
            False,
            {"llm_provider": "anthropic", "llm_model": "replacement"},
            ("openrouter", "saved/model"),
        ),
    ],
)
def test_mobile_explicit_model_selection(
    monkeypatch: pytest.MonkeyPatch,
    mobile: bool,
    overrides: dict,
    expected: tuple[str, str],
) -> None:
    from app import llm
    from app.config import Settings

    for name in ("LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    runtime = Settings(_env_file=None, mobile_api_enabled=mobile, **overrides)
    saved = {"provider": "openrouter", "model": "saved/model"}
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(llm, "load_config_file", lambda: saved.copy())

    def unexpected_write(config: dict) -> None:
        pytest.fail("Runtime selection must not overwrite the saved configuration")

    monkeypatch.setattr(llm, "save_config_file", unexpected_write)
    config = llm.get_llm_config()
    assert (config.provider, config.model) == expected
    assert saved == {"provider": "openrouter", "model": "saved/model"}


def test_mobile_model_selection_reads_railway_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import llm
    from app.config import Settings

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "publisher/environment-model")
    monkeypatch.setenv("LLM_API_KEY", "new-environment-key")
    monkeypatch.setattr(
        llm, "settings", Settings(_env_file=None, mobile_api_enabled=True)
    )
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openrouter",
            "model": "saved/model",
            "api_key": "expired-key",
        },
    )
    config = llm.get_llm_config()
    assert config.provider == "openrouter"
    assert config.model == "publisher/environment-model"
    assert config.api_key == "new-environment-key"
    assert llm.get_model_name(config) == "openrouter/publisher/environment-model"


def test_mobile_provider_switch_does_not_reuse_previous_credentials_or_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import llm
    from app.config import Settings

    runtime = Settings(
        _env_file=None,
        mobile_api_enabled=True,
        llm_provider="openrouter",
        llm_model="publisher/replacement",
        llm_api_key="new-openrouter-key",
        llm_api_base=None,
        reasoning_effort=None,
    )
    saved = {
        "provider": "openai_compatible",
        "model": "old-model",
        "api_key": "old-local-key",
        "api_base": "https://old-endpoint.invalid/v1",
        llm.API_BASE_KEY_BINDING: "old-binding",
        "api_version": "old-version",
        "reasoning_effort": "high",
    }
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(llm, "load_config_file", lambda: saved.copy())
    config = llm.get_llm_config()
    assert (config.provider, config.model) == ("openrouter", "publisher/replacement")
    assert config.api_key == "new-openrouter-key"
    assert config.api_base is None
    assert config.api_version is None
    assert config.reasoning_effort is None
    assert saved["api_key"] == "old-local-key"


def test_mobile_provider_without_model_preserves_saved_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import llm
    from app.config import Settings

    monkeypatch.delenv("LLM_MODEL", raising=False)
    runtime = Settings(
        _env_file=None, mobile_api_enabled=True, llm_provider="openrouter"
    )
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "anthropic",
            "model": "old-provider-model",
        },
    )
    config = llm.get_llm_config()
    assert (config.provider, config.model) == ("anthropic", "old-provider-model")


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_mobile_provider_switch_does_not_leak_cloud_key_to_local_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    from app import llm
    from app.config import Settings

    runtime = Settings(
        _env_file=None,
        mobile_api_enabled=True,
        llm_provider=provider,
        llm_model="local-model",
        llm_api_key="paid-cloud-key",
    )
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openrouter",
            "model": "saved/model",
            "api_key": "old-cloud-key",
        },
    )
    assert llm.get_llm_config().api_key == ""


def test_mobile_runtime_gpt5_selection_does_not_persist_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from app import llm
    from app.config import Settings

    runtime = Settings(
        _env_file=None,
        mobile_api_enabled=True,
        llm_provider="openai",
        llm_model="gpt-5-nano-2025-08-07",
    )
    save = Mock()
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openrouter",
            "model": "saved/model",
        },
    )
    monkeypatch.setattr(llm, "save_config_file", save)
    assert llm.get_llm_config().model == "gpt-5-nano-2025-08-07"
    save.assert_not_called()


@pytest.mark.parametrize(
    ("mobile", "overrides", "saved_effort", "expected"),
    [
        (True, {}, "high", "high"),
        (True, {}, "", None),
        (True, {"reasoning_effort": "minimal"}, "high", "minimal"),
        (True, {"reasoning_effort": "low"}, "high", "low"),
        (True, {"reasoning_effort": "medium"}, "minimal", "medium"),
        (True, {"reasoning_effort": "high"}, "minimal", "high"),
        (True, {"reasoning_effort": ""}, "minimal", None),
        (True, {"reasoning_effort": None}, "minimal", None),
        (False, {"reasoning_effort": "medium"}, "minimal", "minimal"),
        (False, {"reasoning_effort": ""}, "high", "high"),
    ],
)
def test_mobile_reasoning_effort_priority(
    monkeypatch: pytest.MonkeyPatch,
    mobile: bool,
    overrides: dict,
    saved_effort: str,
    expected: str | None,
) -> None:
    from app import llm
    from app.config import Settings

    for name in ("LLM_PROVIDER", "LLM_MODEL", "REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    runtime = Settings(_env_file=None, mobile_api_enabled=mobile, **overrides)
    saved = {
        "provider": "openrouter",
        "model": "saved/model",
        "reasoning_effort": saved_effort,
    }
    monkeypatch.setattr(llm, "settings", runtime)
    monkeypatch.setattr(llm, "load_config_file", lambda: saved.copy())
    assert llm.get_llm_config().reasoning_effort == expected
    assert saved["reasoning_effort"] == saved_effort


def test_mobile_reasoning_environment_reaches_completion_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import llm
    from app.config import Settings

    monkeypatch.setenv("REASONING_EFFORT", "medium")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-haiku-5.5")
    monkeypatch.setattr(
        llm, "settings", Settings(_env_file=None, mobile_api_enabled=True)
    )
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openrouter",
            "model": "saved/model",
            "reasoning_effort": "minimal",
        },
    )
    config = llm.get_llm_config()
    kwargs: dict = {}
    llm._apply_reasoning_effort(config, kwargs, config.reasoning_effort, 8192)
    assert config.reasoning_effort == "medium"
    assert kwargs["extra_body"]["reasoning"]["effort"] == "medium"


@pytest.mark.parametrize("value", ["medium", ""])
def test_mobile_reasoning_override_does_not_trigger_legacy_gpt5_migration(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    from unittest.mock import Mock

    from app import llm
    from app.config import Settings

    for name in ("LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REASONING_EFFORT", value)
    monkeypatch.setattr(
        llm, "settings", Settings(_env_file=None, mobile_api_enabled=True)
    )
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openai",
            "model": "gpt-5-nano-2025-08-07",
        },
    )
    save = Mock()
    monkeypatch.setattr(llm, "save_config_file", save)
    assert llm.get_llm_config().reasoning_effort == (value or None)
    save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
async def test_mobile_openrouter_effort_reaches_unified_wire_format(
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    import json

    import httpx
    import litellm
    import respx

    from app import llm
    from app.config import Settings

    monkeypatch.setenv("REASONING_EFFORT", effort)
    monkeypatch.setattr(
        llm,
        "settings",
        Settings(
            _env_file=None,
            mobile_api_enabled=True,
            llm_provider="openrouter",
            llm_model="anthropic/claude-haiku-5.5",
            llm_api_key="fake-test-key",
            llm_api_base=None,
        ),
    )
    monkeypatch.setattr(
        llm,
        "load_config_file",
        lambda: {
            "provider": "openrouter",
            "model": "old/model",
            "reasoning_effort": "minimal",
        },
    )
    monkeypatch.setattr(llm, "_router", None)
    monkeypatch.setattr(llm, "_router_config_key", "")
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", True, raising=False)
    litellm.in_memory_llm_clients_cache.flush_cache()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "mock-effort",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "anthropic/claude-haiku-5.5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "verified"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        )
        assert await llm.complete("Mocked request", max_tokens=8192) == "verified"
        payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"]["effort"] == effort
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload


def test_openrouter_reasoning_preserves_other_options_and_short_probe_safety() -> None:
    from app import llm

    config = llm.LLMConfig(
        provider="openrouter",
        model="anthropic/claude-haiku-5.5",
        api_key="fake-test-key",
    )
    kwargs: dict = {
        "extra_body": {"reasoning": {"exclude": True, "max_tokens": 2048}},
        "temperature": 0.7,
    }
    llm._apply_reasoning_effort(config, kwargs, "medium", 8192)
    assert kwargs["extra_body"]["reasoning"] == {"exclude": True, "effort": "medium"}
    probe: dict = {}
    llm._apply_reasoning_effort(config, probe, "medium", 64)
    assert probe == {}
