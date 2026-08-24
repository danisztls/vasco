# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from vasco.adapters import deepseek as ds


def test_provider_endpoints_has_deepseek() -> None:
    assert ds.PROVIDER_ENDPOINTS["deepseek"] == "https://api.deepseek.com/v1"


def test_usage_from_response_maps_openai_fields() -> None:
    data = {
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_cache_hit_tokens": 1024,
        }
    }
    assert ds.usage_from_response(data) == {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_read_input_tokens": 1024,
        "cache_creation_input_tokens": None,
        "cost_usd": None,  # OpenAI-compatible providers return no dollar cost
    }


def test_usage_from_response_handles_missing_usage() -> None:
    assert ds.usage_from_response({}) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": None,
    }
