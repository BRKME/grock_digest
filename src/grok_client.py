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
    "required": ["title", "summary", "score"],
    "properties": {
        "title": {"type": "string", "maxLength": 140},
        "summary": {"type": "string", "maxLength": 320},
        "score": {"type": "integer", "minimum": 0},
    },
}


def _bucket(items: int = 2) -> dict:
    return {"type": "array", "minItems": items, "maxItems": items, "items": _ITEM_SCHEMA}


SCHEMA_NEWS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ru_top", "macro"],
    "properties": {"ru_top": _bucket(), "macro": _bucket()},
}

def _financial_schema(third: str) -> dict:
    """Третий бакет — bigtech или pharma — меняется день через день.
    
    Все бакеты по 3 для единообразия и компактности.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["crypto", "stocks", third],
        "properties": {
            "crypto": _bucket(),
            "stocks": _bucket(),
            third: _bucket(),
        },
    }


SCHEMA_THEMATIC = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sports", "ai"],
    "properties": {"sports": _bucket(), "ai": _bucket()},
}


# ---------------------- SHARED RULES ----------------------

_QUALITY_RULES = (
    "Quality rules (MANDATORY — items violating these MUST be replaced):\n\n"
    "1. NO DUPLICATE STORIES IN ONE BUCKET.\n"
    "   If 3 of 5 items are about Apple Vision Pro from different angles "
    "   (new app, screen recording feature, environment design) — keep ONLY "
    "   the most newsworthy one, then find 3 different topics. Same applies "
    "   to: Tesla/Musk daily news, single-company earnings spam, AI lab "
    "   blog posts, single politician's statements. Each item = a clearly "
    "   different sujet (different company OR different angle of broader trend).\n\n"
    "2. NO FLUFF / NO META-NEWS.\n"
    "   Each item MUST report a concrete event from last 24h: who did what, "
    "   when, what changed. FORBIDDEN formulations:\n"
    "   - 'X is under pressure / faces challenges / shows momentum'\n"
    "   - 'Memes and discussions illustrate trend'\n"
    "   - 'Experts debate / users are talking about / community reacts'\n"
    "   - 'Y growing in popularity / gaining traction'\n"
    "   These are NOT news — these are placeholders Grok fills when out of "
    "   real material. If you can't find 4 real concrete events for the bucket, "
    "   leave fewer items (the schema is min=max but expand search broader, "
    "   never fill with fluff).\n\n"
    "3. FACTUAL LANGUAGE, NOT INTERPRETATION.\n"
    "   - GOOD: 'Дженсен Хуан в интервью CNBC сказал, что Qualcomm имеет "
    "     долгосрочный потенциал. $QCOM вырос на 3% после комментария.'\n"
    "   - BAD: 'Дженсен Хуан рекомендовал купить акции Qualcomm.'\n"
    "   Don't promote a comment into a recommendation. Don't promote "
    "   speculation into a fact. Quote-attribute when needed: "
    "   'По словам X, ...', 'По данным Bloomberg/Reuters, ...'.\n\n"
    "4. MAXIMUM 1 item per source account per bucket.\n\n"
    "5. Skip low-signal content: meme reposts of celebrity videos, fan-account "
    "   reactions, generic aesthetic content, PR photo dumps, giveaway/scam "
    "   threads, NSFW/porn-bot spam.\n\n"
    "6. Skip dangerous content: bioweapon how-to, terrorist glorification, "
    "   child exploitation, doxxing, illegal drug/weapon markets.\n"
)

_RUSSIAN_OUTPUT_RULES = (
    "Output language: ALL titles and summaries MUST be in natural Russian, "
    "regardless of source tweet language.\n"
    "Title: clean factual headline in Russian, no clickbait, no emoji.\n"
    "Summary: 1-2 sentences in Russian.\n"
    "score: integer engagement metric used for ranking. Use the LIKES count "
    "of the primary tweet. If likes are unavailable, use floor(views / 10) as "
    "a fallback approximation. Higher = more engaging. Items within each "
    "bucket should be returned sorted DESCENDING by score (highest first).\n"
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
    "- ru_top: топ самых обсуждаемых тем в РУССКОЯЗЫЧНОМ X (lang:ru) за 24ч. "
    "  Общая повестка: политика РФ, общество, культура, происшествия, экономика РФ. "
    "  ВАЖНО: минимум 1-2 пункта из 4 — НЕ про военные действия / войну. "
    "  Если в день много военных новостей — выбери САМОЕ важное одно военное "
    "  событие, остальные слоты заполни не-военными темами: бизнес, наука, "
    "  технологии, культура, общество, происшествия не связанные с фронтом.\n"
    "- macro: топ макро-событий, влияющих на рынки, за 24ч. ГЛОБАЛЬНО, не "
    "  только США. Сюда входит: ФРС/Пауэлл, ЕЦБ/Лагард, Банк России, Банк "
    "  Японии, Банк Англии, Народный банк Китая, инфляция/CPI любых стран "
    "  G10, госдолг США/ЕС, торговые войны и тарифы, санкции, нефтяные шоки, "
    "  геополитические события с рыночным эффектом (Taiwan/Middle East). "
    "  Не больше 2 пунктов из 4 про США. ИСКЛЮЧИ: движения отдельных тикеров "
    "  (это в stocks), чисто крипто-новости (это в crypto).\n\n"
    "Ранжируй по реальной вовлечённости (просмотры + репосты + ответы), "
    "а не по громкости заголовка.\n\n"
    "ИСКЛЮЧИ темы, чьи нормализованные хэши уже в этом списке (были в "
    "дайджесте за последние 48ч):\n{seen}"
)

_SYSTEM_FIN = (
    "You are a financial and industry trends analyst for a Russian-language "
    "Telegram channel. Use the x_search tool to find top trends on X in the "
    "last 24 hours. Cover financial markets, crypto and the specific industry "
    "vertical requested in the user prompt (Big Tech or Pharma).\n\n"
    + _QUALITY_RULES
    + "\n"
    + _RUSSIAN_OUTPUT_RULES
    + "\nOutput ONLY valid JSON matching the provided schema."
)

# BigTech-вариант (как было) — чётные дни года
_USER_FIN_TMPL_BIGTECH = (
    "Дай топ-5 тем за 24ч на X в каждой категории:\n\n"
    "- crypto: рынки криптовалют, on-chain события, движения BTC/ETH/altcoins, "
    "  институциональные новости, регуляторные решения по крипте, ETF.\n"
    "- stocks: фондовый рынок США. Конкретные тикеры (NVDA/TSLA/AAPL/etc), "
    "  движения индексов S&P 500/Nasdaq/Dow, отчётности, IPO/M&A. "
    "  ИСКЛЮЧИ: чистый макро и ФРС (это в macro), AI-релизы моделей "
    "  (это в ai). Если ралли/обвал тикера связан с AI — можно включить "
    "  под углом рынка.\n"
    "- bigtech: Big Tech и НЕ-AI техно-новости. Apple/Google/Microsoft/Meta/"
    "  Amazon/Tesla/NVIDIA/Samsung/Sony — продуктовые анонсы, корпоративные "
    "  решения, антимонопольные дела, hardware-релизы, IPO техно-компаний.\n"
    "  КРИТИЧЕСКИ ВАЖНО — разнообразие компаний:\n"
    "    * максимум 1 пункт от одной компании\n"
    "    * 3 пункта = 3 РАЗНЫЕ компании, не три новости про Tesla или Apple\n"
    "    * если у Tesla сегодня 5 интересных событий и у Microsoft 1 — "
    "      возьми лучшее Tesla + одно Microsoft + что-то третье\n"
    "    * активно ищи новости от менее упоминаемых: Microsoft, Meta, "
    "      Amazon, Samsung, Sony, NVIDIA hardware (без AI), Spotify, "
    "      Netflix, ByteDance, Xiaomi\n"
    "  ИСКЛЮЧИ: релизы AI-моделей и AI-research (это в ai). Сюда — Tesla FSD, "
    "  iPhone, Vision Pro, корпоративные приобретения, закрытия продуктов.\n\n"
    "Не более 1 темы с одного аккаунта в каждой корзине.\n\n"
    "ИСКЛЮЧИ темы с этими хэшами:\n{seen}"
)

# Pharma-вариант — нечётные дни года
_USER_FIN_TMPL_PHARMA = (
    "Дай топ-5 тем за 24ч на X в каждой категории:\n\n"
    "- crypto: рынки криптовалют, on-chain события, движения BTC/ETH/altcoins, "
    "  институциональные новости, регуляторные решения по крипте, ETF.\n"
    "- stocks: фондовый рынок США. Конкретные тикеры (NVDA/TSLA/AAPL/etc), "
    "  движения индексов S&P 500/Nasdaq/Dow, отчётности, IPO/M&A. "
    "  ИСКЛЮЧИ: чистый макро и ФРС, AI-релизы моделей.\n"
    "- pharma: фармацевтический и e-pharma рынок. ПРАВИЛА:\n"
    "    * всего 3 пункта\n"
    "    * минимум 1 из 3 — из России / СНГ (фарма + e-pharma: онлайн-аптеки, "
    "      маркетплейсы лекарств, рецептурные сервисы, регуляторика Минздрава, "
    "      сделки между фарм-компаниями РФ)\n"
    "    * остальные 1-2 — из мира (приоритет США: FDA-решения, IPO биотехов, "
    "      M&A в фарме), но допустимы новости из EU/Asia если сильнее\n"
    "    * если в РФ-сегменте за сутки нет ни одной значимой новости — "
    "      честно бери 0 РФ-пунктов, не выдумывай\n"
    "  Выделяй бизнес-влияние: кто выигрывает, рост продаж, регуляторные "
    "  решения, M&A сделки, IPO биотех-компаний, выход препаратов на рынок, "
    "  ценовые войны, e-commerce фармы.\n"
    "  ИСКЛЮЧИ: общие лайфстайл-материалы про здоровье, статьи о пользе "
    "  витаминов без конкретного бизнес-контекста, мнения без новостной нагрузки.\n\n"
    "Не более 1 темы с одного аккаунта в каждой корзине.\n\n"
    "ИСКЛЮЧИ темы с этими хэшами:\n{seen}"
)


def pick_third_bucket() -> tuple[str, str]:
    """Возвращает (имя_бакета, user_prompt_template) для текущего дня.

    Чередуем по day-of-year (а не по дню недели — так чтобы при пропуске
    запуска ротация не сбивалась):
    - чётный day-of-year → bigtech
    - нечётный          → pharma
    """
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).timetuple().tm_yday
    if day % 2 == 0:
        return "bigtech", _USER_FIN_TMPL_BIGTECH
    else:
        return "pharma", _USER_FIN_TMPL_PHARMA

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
    "  MMA/UFC/бокс, хоккей (NHL/KHL), киберспорт, олимпийские виды.\n"
    "  ПРИОРИТЕТ МЕЙДЖОРАМ: если в это время идёт крупный турнир — "
    "  FIFA World Cup, UEFA EURO, Olympics, Champions League knockout, "
    "  NBA Finals, Stanley Cup Finals, Grand Slam tennis — МИНИМУМ 2 из 3 "
    "  пунктов должны быть про этот турнир (вчерашние матчи, голы, трансферы "
    "  героев турнира). Не сваливай матчи мейджора в одну строку 'результаты "
    "  тура' — каждый ключевой матч/событие — отдельным пунктом.\n"
    "  В это время ИГНОРИРУЙ: товарищеские матчи сборных, второстепенные "
    "  лиги, индивидуальные награды не связанные с мейджором.\n"
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


def call_financial_digest(seen_hashes: list[str]) -> tuple[dict[str, Any], str]:
    """Возвращает (payload, имя_третьего_бакета).
    
    Имя третьего бакета — 'bigtech' или 'pharma' — выбирается по чётности
    дня года. main.py использует его чтобы знать какой шаблон рендерить
    и какие ключи дедупить.
    """
    third, user_tmpl = pick_third_bucket()
    payload = _call(
        system=_SYSTEM_FIN,
        user=user_tmpl.format(seen=seen_hashes[:50] or "[]"),
        schema=_financial_schema(third),
        schema_name=f"financial_digest_{third}",
        call_label=f"B_financial_{third}",
    )
    return payload, third


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
