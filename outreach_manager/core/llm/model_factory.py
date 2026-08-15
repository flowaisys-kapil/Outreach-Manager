# openoutreach/core/llm/model_factory.py
"""Model factory for constructing primary and fallback LLM models."""
from __future__ import annotations

import logging

from pydantic_ai.models.fallback import FallbackModel
from outreach_manager.core.config import get_config
from .utils import split_model_id
from .providers import _SDK_PRIMARY_RETRIES, _SDK_FALLBACK_RETRIES

logger = logging.getLogger(__name__)


def get_llm_model(structured_output: bool = False):
    """Return a configured pydantic-ai ``Model`` for the primary (and optional fallback) provider."""
    import outreach_manager.core.llm as llm

    ai_cfg = get_config().ai

    model_id = ai_cfg.primary_model
    provider, model_name = split_model_id(model_id)
    if ai_cfg.primary_provider and ai_cfg.primary_provider.strip():
        provider = ai_cfg.primary_provider.strip().lower()

    # Dynamic lookup of builder via core.llm to support test patching
    builder_name = f"_build_{provider}"
    builder = getattr(llm, builder_name, None)
    if builder is None:
        raise ValueError(
            f"Unsupported LLM provider '{provider}' in primary_model '{model_id}'."
        )

    api_key = ai_cfg.primary_api_key
    api_base = ai_cfg.primary_api_base

    if not api_key:
        raise ValueError(f"Primary API key is missing for LLM provider '{provider}'.")

    primary_model = builder(model_name, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES)

    backup_key = ai_cfg.fallback_api_key
    if not backup_key:
        return primary_model

    backup_compatible = ai_cfg.backup_structured_output_compatible

    if structured_output and not backup_compatible:
        logger.debug(
            "[LLM] structured_output=True but backup is not flagged compatible "
            "(backup_structured_output_compatible). Using primary model only."
        )
        return primary_model

    backup_model_id = ai_cfg.fallback_model or "openai_compatible:meta/llama-3.1-8b-instruct"
    backup_base = ai_cfg.fallback_api_base or "https://integrate.api.nvidia.com/v1"

    backup_provider, backup_model_name = split_model_id(backup_model_id)
    if ai_cfg.fallback_provider and ai_cfg.fallback_provider.strip():
        backup_provider = ai_cfg.fallback_provider.strip().lower()

    backup_builder_name = f"_build_{backup_provider}"
    backup_builder = getattr(llm, backup_builder_name, None)
    if backup_builder is None:
        logger.warning("[LLM] Backup provider '%s' not found — skipping fallback.", backup_provider)
        return primary_model

    backup_model = backup_builder(backup_model_name, backup_key, backup_base, max_retries=_SDK_FALLBACK_RETRIES)

    logger.info("[LLM] primary=%s (%s), fallback=%s (%s)", model_id, provider, backup_model_id, backup_provider)
    return FallbackModel(primary_model, backup_model)
