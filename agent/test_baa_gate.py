"""Unit tests for BAA compliance gate (no live API)."""

from __future__ import annotations

from unittest.mock import patch

from agent.baa_gate import BAA_REFUSAL_MESSAGE, baa_confirmed


def test_baa_confirmed_defaults_false() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert baa_confirmed() is False


def test_baa_confirmed_truthy_values() -> None:
    for value in ("true", "TRUE", "1", "yes", "on"):
        with patch.dict("os.environ", {"BAA_CONFIRMED": value}, clear=False):
            assert baa_confirmed() is True


def test_baa_confirmed_false_values() -> None:
    for value in ("false", "0", "no", ""):
        with patch.dict("os.environ", {"BAA_CONFIRMED": value}, clear=False):
            assert baa_confirmed() is False


def test_baa_refusal_message_is_fixed() -> None:
    assert "Business Associate Agreement" in BAA_REFUSAL_MESSAGE
    assert "configured LLM provider" in BAA_REFUSAL_MESSAGE
    assert "compliance office or legal counsel" in BAA_REFUSAL_MESSAGE
    assert "Anthropic" not in BAA_REFUSAL_MESSAGE
