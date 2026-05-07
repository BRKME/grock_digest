"""xAI Grok клиент. Два вызова: общие новости (EN+RU) и вертикали.

Использует Responses API с серверным тулом x_search.
SDK: openai (xAI совместим через base_url).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI

from . import telemetry

XAI_API_KEY = os.environ["XAI_API_KEY"]
MODEL_PRIMARY = os.environ.get("GROK_MODEL_PRIMARY", "grok-4.3")
MODEL_FALLBACK = os.environ.get("GROK_MODEL_FALLBACK", "grok-4.20-non-reasoning")
REASONING_EFFORT = os.environ.get("GROK_REASONING_EFFORT", "medium")
MAX_OUTPUT_TOKENS = int(os.environ.get("GROK_MAX_OUTPUT", "2500"))

_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


# ---------------------- SCHEMAS ----------------------

_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "engagement_note", "source_url"],
    "properties": {
        "title": {"type": "string", "maxLength": 140},
        "summary": {"type": "string", "maxLength": 320},
        "engagement_note": {"type": "string", "maxLength": 80},
        "source_url": {"type": "string"},
    },
}

SCHEMA_NEWS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["en_top", "ru_top"],
    "properties": {
        "en_top": {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
        "ru_top": {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
    },
}

SCHEMA_VERTICALS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["crypto", "stocks", "sports", "ai"],
    "properties": {
        "crypto": {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
        "stocks": {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
        "sports": {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
        "ai":     {"type": "array", "minItems": 5, "maxItems": 5, "items": _ITEM_SCHEMA},
    },
}


# ---------------------- PROMPTS ----------------------

_SYSTEM_NEWS = (
    "You are a news curator for a Russian-language Telegram channel. "
    "Use the x_search tool to find the most discussed topics on X (Twitter) "
    "in the last 24 hours. Use lang:en filter for the English bucket and lang:ru "
    "for the Russian bucket. Rank by engagement: views + reposts + replies. "
    "Skip NSFW, graphic violence, porn-bot spam, and clear scam/giveaway threads. "
    "IMPORTANT: ALL titles and summaries MUST be in Russian, regardless of the "
    "source language. For the en_top bucket — translate titles and summaries from "
    "English into natural Russian. For ru_top — keep Russian. "
    "engagement_note format example: '7.1M просмотров, 16K репостов, 6.8K ответов'. "
    "Output ONLY valid JSON matching the provided schema."
)

_USER_NEWS_TMPL = (
    "Найди:\n"
    "- Топ-5 самых обсуждаемых тем в англоязычном X (lang:en) за последние 24 часа.\n"
    "- Топ-5 самых обсуждаемых тем в русскоязычном X (lang:ru) за последние 24 часа.\n\n"
    "Для каждой темы: чистый фактический заголовок на русском (без кликбейта), "
    "1-2 предложения резюме на русском, метрика вовлечённости на русском "
    "(пример: '12M просмотров, 4K репостов'), и URL на x.com.\n\n"
    "ИСКЛЮЧИ темы, чьи нормализованные хэши уже в этом списке (были в дайджесте "
    "за последние 48ч):\n{seen}"
)

_SYSTEM_VERT = (
    "You are a trend analyst for a Russian-language Telegram channel. "
    "Use the x_search tool to find the top 5 trending topics on X (Twitter) "
    "in the last 24 hours for each vertical. "
    "Rank by engagement and discussion velocity. "
    "Skip NSFW and scam content. "
    "IMPORTANT: ALL titles and summaries MUST be written in natural Russian, "
    "regardless of the source language of the underlying tweets. "
    "engagement_note in Russian (e.g. '1.2M просмотров, 3K репостов'). "
    "Output ONLY valid JSON matching the provided schema."
)

_USER_VERT_TMPL = (
    "Дай топ-5 трендов за последние 24ч на X (Twitter) в каждой категории, "
    "ВСЁ НА РУССКОМ ЯЗЫКЕ:\n"
    "- crypto: рынки криптовалют, проекты, on-chain события.\n"
    "- stocks: фондовый рынок США (S&P 500, Nasdaq, отдельные тикеры, ФРС, макро).\n"
    "- sports: любой спорт КРОМЕ американского футбола (NFL/college), гольфа и "
    "  водных видов (плавание, сёрфинг, парусный спорт, водное поло). "
    "  Футбол/баскетбол/теннис/F1/MMA и прочее — можно.\n"
    "- ai: AI/ML — релизы моделей, анонсы лабораторий, исследования, AI-продукты.\n\n"
    "Для каждой темы: фактический заголовок на русском, резюме 1-2 предложения "
    "на русском, метрика вовлечённости на русском, URL на x.com.\n\n"
    "ИСКЛЮЧИ темы с этими нормализованными хэшами (уже были в дайджесте):\n{seen}"
)


# ---------------------- CALLS ----------------------

def call_news_digest(seen_hashes: list[str]) -> dict[str, Any]:
    user_msg = _USER_NEWS_TMPL.format(seen=seen_hashes[:50] or "[]")
    return _call_with_retry(
        system=_SYSTEM_NEWS,
        user=user_msg,
        schema=SCHEMA_NEWS,
        schema_name="news_digest",
        call_label="A_news",
    )


def call_verticals_digest(seen_hashes: list[str]) -> dict[str, Any]:
    user_msg = _USER_VERT_TMPL.format(seen=seen_hashes[:50] or "[]")
    return _call_with_retry(
        system=_SYSTEM_VERT,
        user=user_msg,
        schema=SCHEMA_VERTICALS,
        schema_name="verticals_digest",
        call_label="B_verticals",
    )


def _call_with_retry(
    *, system: str, user: str, schema: dict, schema_name: str, call_label: str,
) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt, model in enumerate([MODEL_PRIMARY, MODEL_PRIMARY, MODEL_FALLBACK]):
        try:
            resp = _client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=[{"type": "x_search"}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
                max_output_tokens=MAX_OUTPUT_TOKENS,
                reasoning={"effort": REASONING_EFFORT},
            )
            payload = json.loads(resp.output_text)
            telemetry.write({
                "call": call_label,
                "model": model,
                "attempt": attempt,
                "input_tokens": _attr(resp.usage, "input_tokens"),
                "output_tokens": _attr(resp.usage, "output_tokens"),
                "cost_in_usd_ticks": _attr(resp.usage, "cost_in_usd_ticks"),
            })
            return payload
        except Exception as e:
            last_err = e
            sleep_s = 2 ** (attempt + 1)
            print(f"[{call_label}] attempt {attempt} on {model} failed: {e!r}, "
                  f"retry in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _attr(obj: Any, name: str) -> Any:
    """Безопасный getattr для usage-полей которые могут отсутствовать у разных моделей."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None
