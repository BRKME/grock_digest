"""Еженедельный отчёт по потребительским трендам — пост в X за 7 дней.

Один Grok-вызов с x_search + web_search, structured JSON, отчёт в @grock.
Расписание: четверг 06:00 UTC = 09:00 MSK.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

from openai import OpenAI

from . import telegram_sender, telemetry

XAI_API_KEY = os.environ["XAI_API_KEY"]
MODEL_PRIMARY = os.environ.get("GROK_MODEL_PRIMARY", "grok-4.3")
MODEL_FALLBACK = os.environ.get("GROK_MODEL_FALLBACK", "grok-4.20-non-reasoning")
REASONING_EFFORT = os.environ.get("GROK_REASONING_EFFORT", "medium")

_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
_MOSCOW = timezone(timedelta(hours=3))


_SYSTEM_PROMPT = (
    "Ты — аналитик по потребительским трендам. Опираясь на публичные посты "
    "в X (Twitter) за последние 7 дней, ищешь зарождающиеся товарные тренды "
    "в массовых категориях (электроника, дом, техника, одежда, дети, "
    "автотовары, спорт, красота, подарки, маркетплейсы). Используй "
    "x_search активно. Аудитория отчёта — селлер/поставщик на маркетплейсе, "
    "который хочет понимать что закупать.\n\n"
    "=== ЖЁСТКИЕ ПРАВИЛА ===\n\n"
    "1. ОБЪЁМ И КАЧЕСТВО:\n"
    "   - Найди 5-7 товарных трендов с явным ростом упоминаний за 7 дней\n"
    "   - Игнорируй темы где меньше 5 разных постов\n"
    "   - Игнорируй очевидную рекламу от ботов\n"
    "   - Различай реальный спрос vs кратковременный хайп/мем/скандал\n\n"
    "2. ПО КАЖДОМУ ТРЕНДУ ОБЯЗАТЕЛЬНО:\n"
    "   - Конкретные ключевые слова/фразы которыми товар обсуждается\n"
    "   - Эмоциональный окрас (восторг / разочарование / сравнение / "
    "     поиск замены / совет)\n"
    "   - 2 коротких ПАРАФРАЗА реальных постов (не выдумывай, не цитируй "
    "     дословно, без имён авторов)\n"
    "   - Импульс: weak / medium / strong\n"
    "   - Риск что это шум: low / medium / high\n\n"
    "3. ОТДЕЛЬНО НАЙДИ:\n"
    "   - Конкретные бренды которые чаще всего всплывают\n"
    "   - Жалобы на дефицит / завышенные цены (с конкретикой)\n"
    "   - Что пользователи советуют как альтернативу\n\n"
    "4. ПИШИ ДЛЯ ЧЕЛОВЕКА:\n"
    "   Не используй маркетинговый жаргон без расшифровки. "
    "   'CTR', 'churn', 'lookalike' — расшифровывай в скобках простыми "
    "   словами. Никаких пустых фраз типа 'тренд растёт' — указывай "
    "   ЧТО ИМЕННО происходит на чём ИМЕННО.\n\n"
    "5. РЕКОМЕНДАЦИЯ В КОНЦЕ:\n"
    "   3 товара/категории на которые селлеру стоит обратить внимание. "
    "   Не общие 'обрати внимание на электронику' — конкретно: какой товар "
    "   и почему именно сейчас.\n\n"
    "Отвечай СТРОГО в JSON по схеме. На русском. Без преамбул."
)


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "trends", "brands", "shortages", "alternatives",
                 "recommendations"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Краткая сводка по неделе в 2-3 предложениях.",
        },
        "trends": {
            "type": "array",
            "minItems": 5,
            "maxItems": 7,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "essence", "keywords", "sentiment",
                             "examples", "impulse", "noise_risk"],
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Категория или конкретный товар",
                    },
                    "essence": {
                        "type": "string",
                        "description": "Суть тренда в 1-2 предложениях",
                    },
                    "keywords": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {"type": "string"},
                    },
                    "sentiment": {
                        "type": "string",
                        "description": "Эмоциональный окрас: восторг / "
                                       "разочарование / сравнение / поиск замены / совет",
                    },
                    "examples": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "string"},
                        "description": "Парафразы 2 реальных постов, без имён",
                    },
                    "impulse": {
                        "type": "string",
                        "enum": ["weak", "medium", "strong"],
                    },
                    "noise_risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Риск что это просто шум/мем а не спрос",
                    },
                },
            },
        },
        "brands": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {"type": "string"},
            "description": "Бренды которые чаще всего всплывают на этой неделе",
        },
        "shortages": {
            "type": "array",
            "minItems": 0,
            "maxItems": 5,
            "items": {"type": "string"},
            "description": "Жалобы на дефицит/завышенные цены, конкретно",
        },
        "alternatives": {
            "type": "array",
            "minItems": 0,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["instead_of", "recommended"],
                "properties": {
                    "instead_of": {"type": "string"},
                    "recommended": {"type": "string"},
                },
            },
        },
        "recommendations": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["product", "why_now"],
                "properties": {
                    "product": {"type": "string"},
                    "why_now": {
                        "type": "string",
                        "description": "Почему именно эту неделю/месяц",
                    },
                },
            },
        },
    },
}


def _build_user_prompt() -> str:
    now = datetime.now(_MOSCOW)
    return (
        f"Сегодня {now.strftime('%d.%m.%Y')}. Аудитория — селлер/поставщик "
        f"на маркетплейсе, ищет что закупать в ближайшие 2-4 недели.\n\n"
        f"Проанализируй посты в X (Twitter) за последние 7 дней и выдай "
        f"еженедельный отчёт по потребительским трендам строго по схеме. "
        f"Фокус — товары которые реально обсуждают: что хвалят, на что "
        f"жалуются, что ищут на замену.\n\n"
        f"Особенно полезны: упоминания маркетплейсов (Wildberries, Ozon, "
        f"Amazon, Я.Маркет), брендов, конкретных моделей товаров; жалобы "
        f"на цены/дефицит; сезонные сдвиги (весна → лето).\n\n"
        f"Если для какого-то тренда не хватает 5+ постов — НЕ включай его. "
        f"Лучше 5 крепких трендов чем 7 слабых."
    )


def call_consumer_trends() -> dict[str, Any]:
    last_err: Exception | None = None
    user_msg = _build_user_prompt()
    for attempt, model in enumerate([MODEL_PRIMARY, MODEL_PRIMARY, MODEL_FALLBACK]):
        try:
            resp = _client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                tools=[
                    {"type": "x_search"},
                    {"type": "web_search"},
                ],
                text={"format": {
                    "type": "json_schema",
                    "name": "consumer_trends",
                    "schema": _SCHEMA,
                    "strict": True,
                }},
                max_output_tokens=5000,
                reasoning={"effort": REASONING_EFFORT},
            )
            text = (resp.output_text or "").strip()
            if not text:
                raise RuntimeError("пустой ответ от Grok")
            parsed = json.loads(text)
            telemetry.write({
                "job": "consumer_trends_weekly",
                "call": "grok",
                "model": model,
                "attempt": attempt,
                "input_tokens": getattr(resp.usage, "input_tokens", None),
                "output_tokens": getattr(resp.usage, "output_tokens", None),
            })
            return parsed
        except Exception as e:
            last_err = e
            sleep_s = 2 ** (attempt + 1)
            print(f"[consumer_trends] attempt {attempt} on {model} failed: {e!r}, "
                  f"retry in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _e(s: Any) -> str:
    import html
    return html.escape("" if s is None else str(s), quote=False)


_IMPULSE_EMOJI = {"weak": "🔸", "medium": "🔶", "strong": "🔥"}
_IMPULSE_LABEL = {"weak": "слабый", "medium": "средний", "strong": "высокий"}
_NOISE_LABEL = {"low": "низкий", "medium": "средний", "high": "высокий"}


def render_report(analysis: dict) -> str:
    now = datetime.now(_MOSCOW)

    lines: list[str] = []
    lines.append(f"🛍 <b>Потребительские тренды — неделя {now.strftime('%d.%m.%Y')}</b>")
    lines.append("")
    lines.append(_e(analysis["summary"]))
    lines.append("")
    lines.append("<b>📈 Тренды недели</b>")

    for i, t in enumerate(analysis["trends"], 1):
        impulse = t.get("impulse", "medium")
        noise = t.get("noise_risk", "medium")
        emoji = _IMPULSE_EMOJI.get(impulse, "🔸")
        imp_label = _IMPULSE_LABEL.get(impulse, impulse)
        noise_label = _NOISE_LABEL.get(noise, noise)

        lines.append("")
        lines.append(f"{emoji} <b>{i}. {_e(t['category'])}</b>")
        lines.append(f"<i>Импульс: {imp_label} · риск шума: {noise_label}</i>")
        lines.append(_e(t["essence"]))
        kw = ", ".join(t.get("keywords") or [])
        if kw:
            lines.append(f"<b>Ключевые слова:</b> {_e(kw)}")
        if t.get("sentiment"):
            lines.append(f"<b>Тон:</b> {_e(t['sentiment'])}")
        for ex in t.get("examples") or []:
            lines.append(f"  «{_e(ex)}»")

    # Brands
    if analysis.get("brands"):
        lines.append("")
        lines.append("<b>🏷 Бренды в обсуждениях</b>")
        lines.append(_e(", ".join(analysis["brands"])))

    # Shortages
    if analysis.get("shortages"):
        lines.append("")
        lines.append("<b>⚠️ Жалобы на дефицит / цены</b>")
        for s in analysis["shortages"]:
            lines.append(f"• {_e(s)}")

    # Alternatives
    if analysis.get("alternatives"):
        lines.append("")
        lines.append("<b>🔄 Что советуют как замену</b>")
        for a in analysis["alternatives"]:
            lines.append(f"• <b>{_e(a['instead_of'])}</b> → {_e(a['recommended'])}")

    # Recommendations
    lines.append("")
    lines.append("<b>🎯 На что обратить внимание селлеру</b>")
    for i, r in enumerate(analysis["recommendations"], 1):
        lines.append(f"<b>{i}. {_e(r['product'])}</b>")
        lines.append(f"   {_e(r['why_now'])}")

    return "\n".join(lines).strip()


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        candidate = (cur + "\n\n" + block) if cur else block
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def run() -> None:
    started = datetime.now(timezone.utc)
    analysis = call_consumer_trends()
    report = render_report(analysis)
    messages = _split_for_telegram(report)
    telegram_sender.send_messages(messages)
    telemetry.write({
        "ts": started.isoformat(),
        "job": "consumer_trends_weekly",
        "trends_count": len(analysis.get("trends") or []),
        "messages_sent": len(messages),
    })


def main() -> int:
    try:
        run()
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            telegram_sender.alert_owner(
                f"❌ consumer_trends_weekly упал:\n<pre>{tb[-2500:]}</pre>"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
