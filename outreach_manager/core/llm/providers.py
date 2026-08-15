# openoutreach/core/llm/providers.py
"""Client builder functions for various LLM providers."""
from __future__ import annotations

from typing import Callable

_SDK_PRIMARY_RETRIES = 2
_SDK_FALLBACK_RETRIES = 1


def _build_openai(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    kwargs = {"api_key": api_key, "max_retries": max_retries, "timeout": timeout}
    if api_base:
        kwargs["base_url"] = api_base
    client = AsyncOpenAI(**kwargs)
    return OpenAIModel(model, provider=OpenAIProvider(openai_client=client))


def _build_anthropic(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    kwargs = {"api_key": api_key, "max_retries": max_retries, "timeout": timeout}
    if api_base:
        kwargs["base_url"] = api_base
    client = AsyncAnthropic(**kwargs)
    return AnthropicModel(model, provider=AnthropicProvider(anthropic_client=client))


def _build_google(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider
    return GoogleModel(model, provider=GoogleProvider(api_key=api_key))


def _build_groq(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from groq import AsyncGroq
    from pydantic_ai.models.groq import GroqModel
    from pydantic_ai.providers.groq import GroqProvider
    client = AsyncGroq(api_key=api_key, max_retries=max_retries, timeout=timeout)
    return GroqModel(model, provider=GroqProvider(groq_client=client))


def _build_mistral(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.providers.mistral import MistralProvider
    return MistralModel(model, provider=MistralProvider(api_key=api_key))


def _build_cohere(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.cohere import CohereModel
    from pydantic_ai.providers.cohere import CohereProvider
    return CohereModel(model, provider=CohereProvider(api_key=api_key))


def _build_openai_compatible(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    if not api_base:
        raise ValueError("LLM_API_BASE is required for the openai_compatible provider.")
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=api_base, api_key=api_key, max_retries=max_retries, timeout=timeout)
    return OpenAIModel(model, provider=OpenAIProvider(openai_client=client))


def _build_nvidia(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    if not api_base:
        api_base = "https://integrate.api.nvidia.com/v1"
    return _build_openai_compatible(model, api_key, api_base, max_retries=max_retries, timeout=timeout)


_PROVIDER_BUILDERS: dict[str, Callable] = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "google": _build_google,
    "nvidia": _build_nvidia,
    "groq": _build_groq,
    "mistral": _build_mistral,
    "cohere": _build_cohere,
    "openai_compatible": _build_openai_compatible,
}
