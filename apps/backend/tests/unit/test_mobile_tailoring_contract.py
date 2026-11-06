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


def test_mobile_provider_switch_requires_explicit_model(
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
    with pytest.raises(ValueError, match="requires an explicit LLM_MODEL"):
        llm.get_llm_config()


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
