"""
Cost Calculator for Workflow Runs

This module provides a comprehensive cost calculation system for workflow runs based on usage metrics
from different AI service providers (OpenAI, Groq, Deepgram, etc.).

Features:
- Token-based pricing for LLM services with cache optimization support
- Character-based pricing for TTS services
- Time-based pricing for STT services
- Configurable pricing models that can be updated
- Support for multiple providers and models
- Automatic provider inference from model names
- JSON serialization support for database storage

Usage:
    from api.tasks.cost_calculator import cost_calculator

    usage_info = {
        "llm": {
            "processor_name|||gpt-4o": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0
            }
        },
        "tts": {
            "processor_name|||aura-2-helena-en": 2000  # character count
        }
    }

    cost_breakdown = cost_calculator.calculate_total_cost(usage_info)
    print(f"Total cost: ${cost_breakdown['total']:.6f}")
"""

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from api.services.configuration.registry import ServiceProviders
from api.services.pricing import PRICING_REGISTRY
from api.services.pricing.models import (
    CharacterPricingModel,
    PricingModel,
    TimePricingModel,
    TokenPricingModel,
)

REALTIME_PROVIDERS = {
    ServiceProviders.OPENAI_REALTIME.value,
    ServiceProviders.GOOGLE_REALTIME.value,
    ServiceProviders.GOOGLE_VERTEX_REALTIME.value,
}

PRICING_SOURCES = {
    ServiceProviders.GROQ.value: {
        "provider": ServiceProviders.GROQ.value,
        "source_url": "https://groq.com/pricing",
        "source_last_checked": "2026-05-22",
    },
    ServiceProviders.GOOGLE.value: {
        "provider": ServiceProviders.GOOGLE.value,
        "source_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "source_last_checked": "2026-05-22",
        "source_last_updated": "2026-05-19",
    },
    ServiceProviders.GOOGLE_REALTIME.value: {
        "provider": ServiceProviders.GOOGLE_REALTIME.value,
        "source_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "source_last_checked": "2026-05-22",
        "source_last_updated": "2026-05-19",
    },
    ServiceProviders.GOOGLE_VERTEX_REALTIME.value: {
        "provider": ServiceProviders.GOOGLE_VERTEX_REALTIME.value,
        "source_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "source_last_checked": "2026-05-22",
        "source_last_updated": "2026-05-19",
    },
}

DEFAULT_USD_INR_RATE = Decimal(os.getenv("USD_INR_RATE", "95.8945"))
USD_INR_RATE_SOURCE = {
    "pair": "USD_INR",
    "rate": float(DEFAULT_USD_INR_RATE),
    "source_url": os.getenv(
        "USD_INR_RATE_SOURCE_URL",
        "https://www.xe.com/currencyconverter/convert/?Amount=1&From=USD&To=INR",
    ),
    "source_last_checked": os.getenv("USD_INR_RATE_SOURCE_LAST_CHECKED", "2026-05-22"),
    "note": "Used to normalize USD-denominated AI API spend into INR for cost analytics.",
}


def _provider_value(provider: str | ServiceProviders) -> str:
    return provider.value if isinstance(provider, ServiceProviders) else str(provider)


def usd_to_inr(amount_usd: Decimal | float | int | str) -> Decimal:
    return Decimal(str(amount_usd)) * DEFAULT_USD_INR_RATE


class CostCalculator:
    """Main cost calculator class"""

    def __init__(self, pricing_registry: Dict = None):
        self.pricing_registry = pricing_registry or PRICING_REGISTRY

    def get_pricing_model(
        self, service_type: str, provider: str, model: str
    ) -> Optional[PricingModel]:
        """Get pricing model for a specific service, provider, and model"""
        try:
            service_pricing = self.pricing_registry.get(service_type, {})

            # Try to get pricing for the specific provider
            provider_pricing = service_pricing.get(provider, {})
            pricing_model = provider_pricing.get(model) or provider_pricing.get(
                "default"
            )

            if pricing_model:
                return pricing_model

            # If not found, try the "default" provider for this service type
            default_provider_pricing = service_pricing.get("default", {})
            return default_provider_pricing.get(model) or default_provider_pricing.get(
                "default"
            )

        except (KeyError, AttributeError):
            return None

    def calculate_llm_cost(
        self, provider: str, model: str, usage: Dict[str, int]
    ) -> Decimal:
        """Calculate cost for LLM usage"""
        pricing_model = self.get_pricing_model("llm", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(usage)

    def calculate_tts_cost(
        self, provider: str, model: str, character_count: int
    ) -> Decimal:
        """Calculate cost for TTS usage"""
        pricing_model = self.get_pricing_model("tts", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(character_count)

    def calculate_stt_cost(self, provider: str, model: str, seconds: float) -> Decimal:
        """Calculate cost for STT usage"""
        pricing_model = self.get_pricing_model("stt", provider, model)
        if not pricing_model:
            return Decimal("0")
        return pricing_model.calculate_cost(seconds)

    def calculate_total_cost(self, usage_info: Dict) -> Dict[str, Any]:
        llm_cost_total = Decimal("0")
        tts_cost_total = Decimal("0")
        stt_cost_total = Decimal("0")

        # Calculate LLM costs
        llm_usage = usage_info.get("llm", {})
        for key, usage in llm_usage.items():
            processor, model = self._parse_key(key)
            provider = self._infer_provider(processor, model, "llm")
            cost = self.calculate_llm_cost(provider, model, usage)
            llm_cost_total += cost

        # Calculate TTS costs
        tts_usage = usage_info.get("tts", {})
        for key, character_count in tts_usage.items():
            processor, model = self._parse_key(key)
            # Handle the case where model is "None" - infer from processor
            if model.lower() in ["none", "null", ""]:
                provider = self._infer_provider_from_processor(processor, "tts")
                model = "default"  # Use default model for the provider
            else:
                provider = self._infer_provider(processor, model, "tts")
            cost = self.calculate_tts_cost(provider, model, character_count)
            tts_cost_total += cost

        # Calculate STT costs from explicit stt usage
        stt_usage = usage_info.get("stt", {})
        for key, seconds in stt_usage.items():
            processor, model = self._parse_key(key)
            provider = self._infer_provider(processor, model, "stt")
            cost = self.calculate_stt_cost(provider, model, seconds)
            stt_cost_total += cost

        total_cost = llm_cost_total + tts_cost_total + stt_cost_total

        return {
            "llm_cost": float(llm_cost_total),
            "tts_cost": float(tts_cost_total),
            "stt_cost": float(stt_cost_total),
            "total": float(total_cost),
        }

    def calculate_actual_cost(
        self,
        usage_info: Dict | None,
        *,
        runtime_configuration: dict | None = None,
        calculated_at: str | None = None,
    ) -> Dict[str, Any]:
        """Return an internal, itemized provider spend snapshot.

        This intentionally tracks real provider spend only.
        """
        usage_info = usage_info or {}
        components: list[dict[str, Any]] = []
        warnings: list[str] = []

        components.extend(
            self._llm_cost_components(usage_info.get("llm", {}), warnings)
        )
        components.extend(
            self._tts_cost_components(usage_info.get("tts", {}), warnings)
        )
        components.extend(
            self._stt_cost_components(usage_info.get("stt", {}), warnings)
        )
        components.extend(
            self._realtime_duration_estimate_components(
                usage_info,
                runtime_configuration=runtime_configuration,
                existing_components=components,
            )
        )

        for item in components:
            cost_usd = Decimal(str(item.get("cost_usd") or 0))
            item["currency"] = item.get("currency") or "USD"
            item["cost_inr"] = float(usd_to_inr(cost_usd))
            if cost_usd:
                item["exchange_rate"] = USD_INR_RATE_SOURCE

        total_usd = sum(Decimal(str(item.get("cost_usd") or 0)) for item in components)
        ai_total_usd = sum(
            Decimal(str(item.get("cost_usd") or 0))
            for item in components
            if item.get("service") != "telephony"
        )
        total_inr = sum(Decimal(str(item.get("cost_inr") or 0)) for item in components)
        ai_total_inr = sum(
            Decimal(str(item.get("cost_inr") or 0))
            for item in components
            if item.get("service") != "telephony"
        )

        sources_by_provider: dict[str, dict[str, str]] = {}
        for item in components:
            provider = str(item.get("provider") or "")
            if provider in PRICING_SOURCES:
                sources_by_provider[provider] = PRICING_SOURCES[provider]

        return {
            "version": 1,
            "currency": "INR",
            "source_currency": "USD",
            "total_usd": float(total_usd),
            "ai_total_usd": float(ai_total_usd),
            "telephony_total_usd": 0.0,
            "total_inr": float(total_inr),
            "ai_total_inr": float(ai_total_inr),
            "telephony_total_inr": 0.0,
            "components": components,
            "estimated": any(bool(item.get("estimated")) for item in components),
            "warnings": warnings,
            "exchange_rates": [USD_INR_RATE_SOURCE],
            "pricing_sources": list(sources_by_provider.values()),
            "calculated_at": calculated_at or datetime.now(UTC).isoformat(),
        }

    def _llm_cost_components(
        self, llm_usage: Dict[str, Dict[str, int]], warnings: list[str]
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []
        for key, usage in llm_usage.items():
            processor, model = self._parse_key(key)
            provider = self._infer_provider(processor, model, "llm")
            pricing_model = self.get_pricing_model("llm", provider, model)
            cost = (
                pricing_model.calculate_cost(usage) if pricing_model else Decimal("0")
            )
            service = (
                "realtime"
                if self._is_realtime_provider_or_model(provider, model)
                else "llm"
            )
            component = self._base_component(
                service=service,
                provider=provider,
                model=model,
                processor=processor,
                cost=cost,
                pricing_model=pricing_model,
                usage={
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens")
                    or 0,
                    "cache_creation_input_tokens": usage.get(
                        "cache_creation_input_tokens"
                    )
                    or 0,
                },
            )
            if not pricing_model:
                warning = (
                    f"No llm pricing configured for provider={_provider_value(provider)} "
                    f"model={model}"
                )
                warnings.append(warning)
                component["warning"] = warning
            if service == "realtime":
                component["estimated"] = True
                component["note"] = (
                    "Realtime token metrics do not include modality-specific token "
                    "counts here, so this line uses text token rates."
                )
            components.append(component)
        return components

    def _tts_cost_components(
        self, tts_usage: Dict[str, int], warnings: list[str]
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []
        for key, character_count in tts_usage.items():
            processor, model = self._parse_key(key)
            if model.lower() in ["none", "null", ""]:
                provider = self._infer_provider_from_processor(processor, "tts")
                model = "default"
            else:
                provider = self._infer_provider(processor, model, "tts")
            pricing_model = self.get_pricing_model("tts", provider, model)
            cost = (
                pricing_model.calculate_cost(character_count)
                if pricing_model
                else Decimal("0")
            )
            component = self._base_component(
                service="tts",
                provider=provider,
                model=model,
                processor=processor,
                cost=cost,
                pricing_model=pricing_model,
                usage={"characters": character_count},
            )
            if not pricing_model:
                warning = (
                    f"No tts pricing configured for provider={_provider_value(provider)} "
                    f"model={model}"
                )
                warnings.append(warning)
                component["warning"] = warning
            components.append(component)
        return components

    def _stt_cost_components(
        self, stt_usage: Dict[str, float], warnings: list[str]
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []
        for key, seconds in stt_usage.items():
            processor, model = self._parse_key(key)
            provider = self._infer_provider(processor, model, "stt")
            pricing_model = self.get_pricing_model("stt", provider, model)
            cost = (
                pricing_model.calculate_cost(seconds) if pricing_model else Decimal("0")
            )
            component = self._base_component(
                service="stt",
                provider=provider,
                model=model,
                processor=processor,
                cost=cost,
                pricing_model=pricing_model,
                usage={"seconds": seconds},
            )
            if not pricing_model:
                warning = (
                    f"No stt pricing configured for provider={_provider_value(provider)} "
                    f"model={model}"
                )
                warnings.append(warning)
                component["warning"] = warning
            components.append(component)
        return components

    def _realtime_duration_estimate_components(
        self,
        usage_info: Dict,
        *,
        runtime_configuration: dict | None,
        existing_components: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        runtime_configuration = runtime_configuration or {}
        provider = runtime_configuration.get("realtime_provider")
        model = runtime_configuration.get("realtime_model")
        if not provider or not model:
            return []

        provider_value = _provider_value(provider)
        if any(
            item.get("service") == "realtime"
            and item.get("provider") == provider_value
            and item.get("model") == model
            for item in existing_components
        ):
            return []

        duration_seconds = float(usage_info.get("call_duration_seconds") or 0)
        if duration_seconds <= 0:
            return []

        # Gemini 3.1 Live exposes per-minute audio rates. This is an internal
        # spend estimate when the pipeline only persisted call duration.
        if (
            provider_value == ServiceProviders.GOOGLE_REALTIME.value
            and model == "gemini-3.1-flash-live-preview"
        ):
            minutes = Decimal(str(duration_seconds)) / Decimal("60")
            input_cost = minutes * Decimal("0.005")
            output_cost = minutes * Decimal("0.018")
            return [
                {
                    "service": "realtime",
                    "provider": provider_value,
                    "model": model,
                    "processor": "runtime_configuration",
                    "label": "Gemini 3.1 Flash Live audio estimate",
                    "cost_usd": float(input_cost + output_cost),
                    "estimated": True,
                    "usage": {
                        "duration_seconds": duration_seconds,
                        "estimated_input_audio_minutes": float(minutes),
                        "estimated_output_audio_minutes": float(minutes),
                    },
                    "pricing": {
                        "input_audio_usd_per_minute": 0.005,
                        "output_audio_usd_per_minute": 0.018,
                    },
                    "source_url": PRICING_SOURCES[provider_value]["source_url"],
                    "note": (
                        "Estimated from full call duration because separate "
                        "Gemini input/output audio minutes were not persisted."
                    ),
                }
            ]
        return []

    def _base_component(
        self,
        *,
        service: str,
        provider: str | ServiceProviders,
        model: str,
        processor: str,
        cost: Decimal,
        pricing_model: PricingModel | None,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        provider_value = _provider_value(provider)
        component = {
            "service": service,
            "provider": provider_value,
            "model": model,
            "processor": processor,
            "cost_usd": float(cost),
            "usage": usage,
            "priced": pricing_model is not None,
        }
        pricing = self._pricing_metadata(pricing_model)
        if pricing:
            component["pricing"] = pricing
        source = PRICING_SOURCES.get(provider_value)
        if source:
            component["source_url"] = source["source_url"]
        return component

    def _pricing_metadata(
        self, pricing_model: PricingModel | None
    ) -> dict[str, float | str] | None:
        if pricing_model is None:
            return None
        if isinstance(pricing_model, TokenPricingModel):
            return {
                "unit": "tokens",
                "prompt_usd_per_1m_tokens": float(
                    pricing_model.prompt_token_price * Decimal("1000000")
                ),
                "completion_usd_per_1m_tokens": float(
                    pricing_model.completion_token_price * Decimal("1000000")
                ),
            }
        if isinstance(pricing_model, CharacterPricingModel):
            return {
                "unit": "characters",
                "usd_per_1k_characters": float(
                    pricing_model.character_price * Decimal("1000")
                ),
            }
        if isinstance(pricing_model, TimePricingModel):
            return {
                "unit": "seconds",
                "usd_per_minute": float(pricing_model.second_price * Decimal("60")),
            }
        return {"unit": "unknown"}

    def _parse_key(self, key) -> Tuple[str, str]:
        """Parse key which is in format 'processor|||model'"""
        if isinstance(key, str) and "|||" in key:
            parts = key.split("|||", 1)
            return parts[0], parts[1]
        else:
            # Fallback for backwards compatibility or malformed keys
            return str(key), "unknown"

    def _infer_provider(self, processor: str, model: str, service_type: str) -> str:
        """Infer provider using processor first, then model name."""
        processor_provider = self._infer_provider_from_processor(
            processor, service_type
        )
        if processor_provider != "unknown":
            return processor_provider
        return self._infer_provider_from_model(model, service_type)

    def _infer_provider_from_model(self, model: str, service_type: str) -> str:
        """Infer provider from model name"""
        if not model:
            return "unknown"

        model_lower = model.lower()

        # Realtime providers
        if "openai" in model_lower and "realtime" in model_lower:
            return ServiceProviders.OPENAI_REALTIME
        if "gemini" in model_lower and "live" in model_lower:
            if model_lower.startswith("google/"):
                return ServiceProviders.GOOGLE_VERTEX_REALTIME
            return ServiceProviders.GOOGLE_REALTIME

        # Google Gemini models
        if "gemini" in model_lower:
            return ServiceProviders.GOOGLE

        # Groq models
        if any(
            keyword in model_lower
            for keyword in [
                "groq",
                "llama",
                "deepseek",
                "qwen",
                "gemma",
                "gpt-oss",
            ]
        ):
            return ServiceProviders.GROQ

        # OpenAI models
        if any(keyword in model_lower for keyword in ["gpt", "whisper", "openai"]):
            return ServiceProviders.OPENAI

        # Elevenlabs models
        if any(keyword in model_lower for keyword in ["eleven"]):
            return ServiceProviders.ELEVENLABS

        # Deepgram models
        if any(
            keyword in model_lower
            for keyword in ["deepgram", "nova", "phonecall", "general"]
        ):
            return ServiceProviders.DEEPGRAM

        return "unknown"

    def _infer_provider_from_processor(self, processor: str, service_type: str) -> str:
        """Infer provider from processor name"""
        if not processor:
            return "unknown"

        processor_lower = processor.lower()

        # Realtime processors
        if "openai" in processor_lower and "realtime" in processor_lower:
            return ServiceProviders.OPENAI_REALTIME
        if any(
            keyword in processor_lower
            for keyword in ["google_vertex_realtime", "geminilivevertex", "vertex"]
        ):
            return ServiceProviders.GOOGLE_VERTEX_REALTIME
        if any(
            keyword in processor_lower
            for keyword in ["google_realtime", "gemini_live", "geminilive"]
        ):
            return ServiceProviders.GOOGLE_REALTIME

        # Google processors
        if any(keyword in processor_lower for keyword in ["google", "gemini"]):
            return ServiceProviders.GOOGLE

        # OpenAI processors
        if any(keyword in processor_lower for keyword in ["openai", "gpt"]):
            return ServiceProviders.OPENAI

        # Groq processors
        if any(keyword in processor_lower for keyword in ["groq"]):
            return ServiceProviders.GROQ

        # Deepgram processors
        if any(keyword in processor_lower for keyword in ["deepgram"]):
            return ServiceProviders.DEEPGRAM

        return "unknown"

    def _is_realtime_provider_or_model(
        self, provider: str | ServiceProviders, model: str
    ) -> bool:
        provider_value = _provider_value(provider)
        model_lower = (model or "").lower()
        return provider_value in REALTIME_PROVIDERS or any(
            keyword in model_lower for keyword in ["realtime", "live"]
        )

    def update_pricing(
        self, service_type: str, provider: str, model: str, pricing_model: PricingModel
    ):
        """Update pricing for a specific service/provider/model combination"""
        if service_type not in self.pricing_registry:
            self.pricing_registry[service_type] = {}
        if provider not in self.pricing_registry[service_type]:
            self.pricing_registry[service_type][provider] = {}
        self.pricing_registry[service_type][provider][model] = pricing_model


# Global cost calculator instance
cost_calculator = CostCalculator()
