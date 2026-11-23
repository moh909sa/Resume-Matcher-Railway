"""Consent routing must follow the approved disclosure before handing data off."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.llm import LLMConfig, _route_acompletion
from app.mobile_consent import consent_scope, current_disclosure


async def test_pins_openrouter_recipient_and_blocks_withdrawal_or_route_change(
    monkeypatch,
):
    config = LLMConfig(provider="openrouter", model="vendor/example", api_key="fixture")
    notice = {
        "provider": config.provider,
        "model": config.model,
        "recipients": ["OpenRouter", "Example provider"],
        "retention": "Test disclosure.",
        "openrouter_only": ["example"],
    }
    monkeypatch.setenv("MOBILE_AI_DISCLOSURE", json.dumps(notice))
    _, version = current_disclosure(config)
    backend = AsyncMock()
    backend.ai_consent.return_value = True
    transport = AsyncMock()

    owner = "760c63e9-c782-4631-a1fb-471e988740a1"
    with consent_scope(backend, owner, version):
        await _route_acompletion(config, transport, model="resume-matcher")
        backend.ai_consent.assert_awaited_once_with(owner, version)
        assert transport.acompletion.await_args.kwargs["extra_body"] == {
            "provider": {"only": ["example"], "allow_fallbacks": False},
        }

        backend.ai_consent.return_value = False
        with pytest.raises(HTTPException) as refused:
            await _route_acompletion(config, transport, model="resume-matcher")
        assert refused.value.status_code == 428

        backend.ai_consent.return_value = True
        monkeypatch.setenv(
            "MOBILE_AI_DISCLOSURE", json.dumps({**notice, "retention": "Updated."})
        )
        with pytest.raises(HTTPException) as changed:
            await _route_acompletion(config, transport, model="resume-matcher")
        assert changed.value.status_code == 428

        monkeypatch.delenv("MOBILE_AI_DISCLOSURE")
        with pytest.raises(HTTPException) as unavailable:
            await _route_acompletion(config, transport, model="resume-matcher")
        assert unavailable.value.status_code == 503

    transport.acompletion.assert_awaited_once()
