"""xAI Grok клиент. Три вызова под 7 категорий:

1. call_news       → ru_top + macro
2. call_financial  → crypto + stocks + bigtech
3. call_thematic   → sports + ai

Использует Responses API + серверный тул x_search.
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
MAX_OUTPUT_TOKENS = int(os.environ.get("GROK_MAX_OUTPUT", "3000"))

_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


# ---------------------- SCHEMA ITEM ----------------------

_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "engagement_note"],
    "properties": {
        "title": {"type": "string", "maxLength": 140},
        "summary": {"type": "string", "maxLength": 320},
        "engagement_note": {"type": "string", "maxLength": 80},
    },
}


def _bucket(items: int = 5) -> dict:
    return {"type": "array", "minItems": items, "maxItems": items, "items": _ITEM_SCHEMA}


SCHEMA_NEWS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ru_top", "macro"],
    "properties": {"ru_top": _bucket(), "macro": _bucket()},
}

SCHEMA_FINANCIAL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["crypto", "stocks", "bigtech"],
    "properties": {"crypto": _bucket(), "stocks": _bucket(), "bigtech": _bucket()},
}

SCHEMA_THEMATIC = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sports", "ai"],
    "properties": {"sports": _bucket(), "ai": _bucket()},
}


# ---------------------- SHARED RULES ----------------------

_QUALITY_RULES = (
    "Quality rules (MANDATORY):\n"
    "- Maximum 1 item per source brand/account per bucket "
    "  (no two items from the same X handle in the same bucket).\n"
    "- Skip low-signal content: meme reposts of celebrity videos, fan-account "
    "  reaction posts, generic cute/aesthetic content, simple PR photo dumps "
    "  (e.g. 'team posted some good vibes photos'), giveaway/scam threads.\n"
    "- Skip dangerous content: bioweapon creation/transmission how-to, "
    "  terrorist attack glorification, child exploitation, doxxing, illegal "
    "  drug/weapon marketplaces, technical instructions for harm.\n"
    "- Skip NSFW and porn-bot spam.\n"
    "- Each item must be a distinct STORY, not a slight variation of the same event.\n"
)

_RUSSIAN_OUTPUT_RULES = (
    "Output language: ALL titles, summaries and engagement_note MUST be "
    "in natural Russian, regardless of source tweet language.\n"
    "engagement_note format: number + unit in Russian. "
    "Use 'M просмотров' for >=1 000 000, 'K просмотров' for 1 000-999 999, "
    "exact number for <1 000. Same logic for 'репостов', 'лайков', 'ответов'. "
    "Combine 2-3 most relevant metrics, comma-separated.\n"
    "Examples: '12.4M просмотров, 3.2K репостов, 1.1K ответов', "
    "'804K просмотров, 12.6K лайков'.\n"
    "Title: clean factual headline in Russian, no clickbait, no emoji.\n"
    "Summary: 1-2 sentences in Russian.\n"
)


# ---------------------- PROMPTS ----------------------

_SYSTEM_NEWS = (
    "You are a news curator for a Russian-language Telegram channel. "
    "Use the x_search tool to find the most discussed topics on X (Twitter) "
    "in the last 24 hours.\n\n"
    + _QUALITY_RULES
    + "\n"
    + _RUSSIAN_OUTPUT_RULES
    + "\nOutput ONLY valid JSON matching the provided schema."
)

_USER_NEWS_TMPL = (
    "Найди:\n"
    "- ru_top: топ-5 самых обсуждаемых тем в РУССКОЯЗЫЧНОМ X (lang:ru) за 24ч. "
    "  Общая повестка: политика РФ, общество, культура, происшествия, экономика РФ.\n"
    "- macro: топ-5 макро-событий, влияющих на рынки, за 24ч. "
    "  Сюда входит: ФРС/Пауэлл, инфляция/CPI/PCE/jobs, госдолг США, "
    "  торговые войны и тарифы, санкции, нефтяные шоки, геополитические "
    "  события с прямым рыночным эффектом (oil/gas/Taiwan/Middle East). "
    "  ИСКЛЮЧИ: движения отдельных тикеров (это в stocks), чисто крипто-новости "
    "  (это в crypto).\n\n"
    "Не более 1 темы с одного аккаунта в каждой корзине.\n"
    "Ранжируй по реальной вовлечённости (просмотры + репосты + ответы), "
    "а не по громкости заголовка.\n\n"
    "ИСКЛЮЧИ темы, чьи нормализованные хэши уже в этом списке (были в "
    "дайджесте за последние 48ч):\n{seen}"
)

_SYSTEM_FIN = (
    "You are a financial trends analyst for a Russian-language Telegram channel. "
    "Use the x_search tool to find top trends on X in the last 24 hours.\n\n"
    + _QUALITY_RULES
    + "\n"
    + _RUSSIAN_OUTPUT_RULES
    + "\nOutput ONLY valid JSON matching the provided schema."
)

_USER_FIN_TMPL = (
    "Дай топ-5 тем за 24ч на X в каждой категории:\n\n"
    "- crypto: рынки криптовалют, on-chain события, движения BTC/ETH/altcoins, "
    "  институциональные новости, регуляторные решения по крипте, ETF.\n"
    "- stocks: фондовый рынок США. Конкретные тикеры (NVDA/TSLA/AAPL/etc), "
    "  движения индексов S&P 500/Nasdaq/Dow, отчётности, IPO/M&A. "
    "  ИСКЛЮЧИ: чистый макро и ФРС (это в macro), AI-релизы моделей "
    "  (это в ai). Если ралли/обвал тикера связан с AI — можно включить "
    "  под углом рынка.\n"
    "- bigtech: Big Tech и НЕ-AI техно-новости. Apple/Google/Microsoft/Meta/"
    "  Amazon/Tesla/NVIDIA — продуктовые анонсы, корпоративные решения, "
    "  антимонопольные дела, hardware-релизы, IPO техно-компаний. "
    "  ИСКЛЮЧИ: релизы AI-моделей и AI-research (это в ai). Сюда — Tesla FSD, "
    "  iPhone, Vision Pro, корпоративные приобретения, закрытия продуктов.\n\n"
    "Не более 1 темы с одного аккаунта в каждой корзине.\n\n"
    "ИСКЛЮЧИ темы с этими хэшами:\n{seen}"
)

_SYSTEM_THEM = (
    "You are a sports & AI trends analyst for a Russian-language Telegram channel. "
    "Use the x_search tool to find top trends on X in the last 24 hours.\n\n"
    + _QUALITY_RULES
    + "\n"
    + _RUSSIAN_OUTPUT_RULES
    + "\nOutput ONLY valid JSON matching the provided schema."
)

_USER_THEM_TMPL = (
    "Дай топ-5 тем за 24ч на X в каждой категории:\n\n"
    "- sports: любой спорт КРОМЕ американского футбола (NFL и college football), "
    "  гольфа и водных видов (плавание, сёрфинг, парусный спорт, водное поло). "
    "  Можно: футбол (soccer), баскетбол (NBA/EuroLeague), теннис, F1/MotoGP, "
    "  MMA/UFC/бокс, хоккей (NHL/KHL), киберспорт, олимпийские виды. "
    "  Приоритет — конкретные матчи, результаты, трансферы, награды. "
    "  ИСКЛЮЧИ: PR-фото клубов без новостной нагрузки, видео-нарезки "
    "  'best moments', fan-account reactions без подтверждённого факта.\n"
    "- ai: AI/ML индустрия — релизы моделей (GPT/Claude/Gemini/Llama/etc), "
    "  анонсы лабораторий (OpenAI/Anthropic/Google DeepMind/xAI/Meta AI), "
    "  AI-исследования и статьи, AI-продукты и интеграции, "
    "  компьютные/инфраструктурные сделки.\n\n"
    "Не более 1 темы с одного аккаунта в каждой корзине.\n\n"
    "ИСКЛЮЧИ темы с этими хэшами:\n{seen}"
)


# ---------------------- CALLS ----------------------

def call_news_digest(seen_hashes: list[str]) -> dict[str, Any]:
    return _call(
        system=_SYSTEM_NEWS,
        user=_USER_NEWS_TMPL.format(seen=seen_hashes[:50] or "[]"),
        schema=SCHEMA_NEWS,
        schema_name="news_digest",
        call_label="A_news",
    )


def call_financial_digest(seen_hashes: list[str]) -> dict[str, Any]:
    return _call(
        system=_SYSTEM_FIN,
        user=_USER_FIN_TMPL.format(seen=seen_hashes[:50] or "[]"),
        schema=SCHEMA_FINANCIAL,
        schema_name="financial_digest",
        call_label="B_financial",
    )


def call_thematic_digest(seen_hashes: list[str]) -> dict[str, Any]:
    return _call(
        system=_SYSTEM_THEM,
        user=_USER_THEM_TMPL.format(seen=seen_hashes[:50] or "[]"),
        schema=SCHEMA_THEMATIC,
        schema_name="thematic_digest",
        call_label="C_thematic",
    )


def _call(
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
    try:
        return getattr(obj, name, None)
    except Exception:
        return None
