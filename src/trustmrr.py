"""Пятничный отчёт по TrustMRR — топ стартапы + AI-анализ трендов через Grok.

Источник: официальный API TrustMRR (https://trustmrr.com/api/v1/startups).
Требует API key в env TRUSTMRR_API_KEY (получить на trustmrr.com → Dashboard).

Поток:
1. Запросить топ-100 стартапов по revenue-desc
2. Отфильтровать high-regulation ниши (GLP-1, EHR с PHI)
3. Передать в Grok с твоим аналитическим промптом
4. Получить структурированный JSON
5. Отрендерить в Telegram-сообщение по твоему шаблону
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from openai import OpenAI

from . import telegram_sender, telemetry

XAI_API_KEY = os.environ["XAI_API_KEY"]
TRUSTMRR_API_KEY = os.environ.get("TRUSTMRR_API_KEY")
MODEL_PRIMARY = os.environ.get("GROK_MODEL_PRIMARY", "grok-4.3")
MODEL_FALLBACK = os.environ.get("GROK_MODEL_FALLBACK", "grok-4.20-non-reasoning")
REASONING_EFFORT = os.environ.get("GROK_REASONING_EFFORT", "medium")

_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
_MOSCOW = timezone(timedelta(hours=3))

_TRUSTMRR_URL = "https://trustmrr.com/api/v1/startups"


# Ниши с высоким регуляторным риском — исключаем из анализа основных трендов
_HIGH_REG_KEYWORDS = (
    # GLP-1 / weight loss / telehealth с рецептами
    "glp-1", "glp1", "weight loss", "semaglutide", "tirzepatide", "ozempic",
    "telehealth", "prescription", "rx ", " rx",
    # EHR с чувствительными данными
    "ehr ", " ehr", "electronic health record", "i/dd",
    "intellectual and developmental disab",
    # Прочая медицина с PHI
    "hormone prescribing",
)


def _is_high_regulation(startup: dict) -> bool:
    """Проверка по описанию (case-insensitive)."""
    desc = (startup.get("description") or "").lower()
    name = (startup.get("name") or "").lower()
    haystack = desc + " " + name
    return any(kw in haystack for kw in _HIGH_REG_KEYWORDS)


def _coerce_number(v: Any) -> float:
    """API может вернуть число, строку или dict с подполем. Берём что есть."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "").replace("%", "").strip())
        except ValueError:
            return 0.0
    if isinstance(v, dict):
        # Пробуем популярные имена подполей по приоритету
        for k in ("amount", "value", "current", "usd", "cents", "total", "monthly"):
            if k in v:
                return _coerce_number(v[k])
    return 0.0


def fetch_top_startups(limit: int = 100) -> list[dict]:
    """Получить топ N стартапов по revenue. API возвращает деньги в центах."""
    if not TRUSTMRR_API_KEY:
        raise RuntimeError(
            "TRUSTMRR_API_KEY не установлен. "
            "Получи ключ на trustmrr.com → Dashboard → API keys, "
            "положи в GitHub Secrets репозитория."
        )

    all_items: list[dict] = []
    page = 1
    per_page = 50  # API лимит обычно 50-100

    while len(all_items) < limit:
        r = requests.get(
            _TRUSTMRR_URL,
            params={
                "sort": "revenue-desc",
                "limit": min(per_page, limit - len(all_items)),
                "page": page,
            },
            headers={"Authorization": f"Bearer {TRUSTMRR_API_KEY}"},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data") or []
        if not items:
            break

        # Один раз сбрасываем структуру первого item — поможет диагностировать
        # любые будущие изменения схемы API без угадывания.
        if page == 1 and items:
            sample_keys = sorted(items[0].keys())
            print(f"[trustmrr] sample item keys: {sample_keys}", flush=True)
            print(f"[trustmrr] sample item (first 500 chars): "
                  f"{json.dumps(items[0], ensure_ascii=False)[:500]}", flush=True)

        all_items.extend(items)
        meta = payload.get("meta") or {}
        if not meta.get("hasMore"):
            break
        page += 1
        time.sleep(0.5)  # вежливость

    # Нормализация: cents → USD, обрезка описания
    out = []
    for s in all_items[:limit]:
        # Revenue/MRR могут быть числом, строкой или вложенным объектом —
        # используем _coerce_number чтобы не падать
        revenue_raw = _coerce_number(s.get("revenue"))
        mrr_raw = _coerce_number(s.get("mrr")) or revenue_raw
        # API документация говорит "values in USD cents", но если число
        # уже выглядит маленьким (<10000), скорее всего это уже USD
        revenue_usd = revenue_raw / 100 if revenue_raw > 10000 else revenue_raw
        mrr_usd = mrr_raw / 100 if mrr_raw > 10000 else mrr_raw

        growth = _coerce_number(
            s.get("growth")
            or s.get("monthlyGrowthPct")
            or s.get("monthlyGrowth")
            or s.get("mom")
        )

        desc = s.get("description") or s.get("tagline") or s.get("about") or ""
        desc = str(desc)[:200]

        out.append({
            "name": str(s.get("name") or s.get("title") or "Unknown"),
            "category": str(s.get("category") or s.get("categoryName") or ""),
            "mrr": round(mrr_usd, 2),
            "revenue": round(revenue_usd, 2),
            "growth": round(growth, 2),
            "description": desc.strip(),
            "on_sale": bool(s.get("onSale") or s.get("isForSale") or s.get("for_sale")),
        })
    return out


def filter_for_analysis(startups: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить на (основной анализ, исключённые регуляторные)."""
    main = []
    excluded = []
    for s in startups:
        if _is_high_regulation(s):
            excluded.append(s)
        else:
            main.append(s)
    return main, excluded


# === Grok analysis ===

_SYSTEM_PROMPT = (
    "Ты — аналитик стартап-трендов с фокусом на indie hacker / bootstrap сегмент. "
    "Тебе передают список из топ стартапов TrustMRR с verified Stripe revenue. "
    "Твоя задача — найти РАСТУЩИЕ ниши, рабочие бизнес-модели и неочевидные пересечения.\n\n"
    "Анализируй ВНИМАТЕЛЬНО:\n"
    "- Растущие = высокий MoM growth (>15%), не просто большой MRR\n"
    "- Бизнес-модели = что общего у тех кто растёт быстро\n"
    "- Паттерны = повторяющиеся технологические темы (AI-native, AEO, automation)\n"
    "- Не пересказывай данные — синтезируй insights, которые не видны из таблицы\n\n"
    "Отвечай СТРОГО в JSON по предоставленной схеме. На русском языке. "
    "Никаких преамбул, никакого Markdown в JSON-полях."
)


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "main_trend", "top_trends", "high_growth_models",
        "aeo_block", "avoid", "weekly_experiment", "prediction",
    ],
    "properties": {
        "main_trend": {
            "type": "string",
            "description": "Главный тренд недели в 1-2 предложениях.",
        },
        "top_trends": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["niche", "examples", "avg_growth", "driver"],
                "properties": {
                    "niche": {"type": "string"},
                    "examples": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                    "avg_growth": {
                        "type": "string",
                        "description": "Средний MoM рост в % (строкой, например '24%').",
                    },
                    "driver": {
                        "type": "string",
                        "description": "Одно предложение — почему растёт.",
                    },
                },
            },
        },
        "high_growth_models": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["model", "example"],
                "properties": {
                    "model": {"type": "string"},
                    "example": {"type": "string"},
                },
            },
        },
        "aeo_block": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tools", "tactic"],
            "properties": {
                "tools": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "string"},
                    "description": "AEO/LLM-visibility инструменты из списка.",
                },
                "tactic": {
                    "type": "string",
                    "description": "Конкретная тактика которую можно скопировать.",
                },
            },
        },
        "avoid": {
            "type": "array",
            "minItems": 2,
            "maxItems": 5,
            "items": {"type": "string"},
            "description": "Что не брать — высокий риск, слабые модели.",
        },
        "weekly_experiment": {
            "type": "string",
            "description": "Одна конкретная гипотеза для теста на этой неделе.",
        },
        "prediction": {
            "type": "string",
            "description": "Прогноз на следующую неделю в 1 предложении.",
        },
    },
}


def _build_user_prompt(startups: list[dict], excluded_count: int) -> str:
    """Компактный JSON-payload для Grok."""
    data = [
        {
            "name": s["name"],
            "mrr": s["mrr"],
            "growth_pct": s["growth"],
            "category": s["category"],
            "desc": s["description"],
        }
        for s in startups
    ]
    return (
        f"Сегодня {datetime.now(_MOSCOW).strftime('%d.%m.%Y')}.\n\n"
        f"Список топ-{len(startups)} стартапов с TrustMRR (отфильтровано: "
        f"{excluded_count} GLP-1/EHR/телемедицина исключены отдельно):\n\n"
        f"{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
        "Сделай еженедельный аналитический отчёт строго по схеме."
    )


def call_grok_analysis(startups: list[dict], excluded_count: int) -> dict[str, Any]:
    last_err: Exception | None = None
    user_msg = _build_user_prompt(startups, excluded_count)
    for attempt, model in enumerate([MODEL_PRIMARY, MODEL_PRIMARY, MODEL_FALLBACK]):
        try:
            resp = _client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                text={"format": {
                    "type": "json_schema",
                    "name": "trustmrr_analysis",
                    "schema": _SCHEMA,
                    "strict": True,
                }},
                max_output_tokens=3500,
                reasoning={"effort": REASONING_EFFORT},
            )
            text = (resp.output_text or "").strip()
            if not text:
                raise RuntimeError("пустой ответ от Grok")
            parsed = json.loads(text)
            telemetry.write({
                "job": "trustmrr_weekly",
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
            print(f"[trustmrr] attempt {attempt} on {model} failed: {e!r}, "
                  f"retry in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


# === Render ===

def _e(s: Any) -> str:
    import html
    return html.escape("" if s is None else str(s), quote=False)


def render_report(analysis: dict, total_count: int, excluded_count: int) -> str:
    now = datetime.now(_MOSCOW)

    lines: list[str] = []
    lines.append(f"📊 <b>Еженедельный отчёт TrustMRR — {now.strftime('%d.%m.%Y')}</b>")
    lines.append(f"<i>Проанализировано: {total_count} стартапов "
                 f"(исключено {excluded_count} регуляторных)</i>")
    lines.append("")
    lines.append("<b>🎯 Главный тренд недели</b>")
    lines.append(_e(analysis["main_trend"]))
    lines.append("")
    lines.append("<b>🚀 Топ-3 растущие ниши</b>")
    for i, t in enumerate(analysis["top_trends"], 1):
        examples = ", ".join(t["examples"])
        lines.append(f"<b>{i}. {_e(t['niche'])}</b> · рост ~{_e(t['avg_growth'])}")
        lines.append(f"   {_e(t['driver'])}")
        lines.append(f"   <i>Примеры: {_e(examples)}</i>")
    lines.append("")
    lines.append("<b>💰 Модели роста (что работает)</b>")
    for m in analysis["high_growth_models"]:
        lines.append(f"• <b>{_e(m['model'])}</b> — {_e(m['example'])}")
    lines.append("")
    lines.append("<b>🤖 AEO & LLM-видимость</b>")
    aeo = analysis["aeo_block"]
    if aeo.get("tools"):
        lines.append(f"Инструменты: {_e(', '.join(aeo['tools']))}")
    lines.append(f"<b>Тактика:</b> {_e(aeo['tactic'])}")
    lines.append("")
    lines.append("<b>🚫 Что НЕ брать</b>")
    for x in analysis["avoid"]:
        lines.append(f"• {_e(x)}")
    lines.append("")
    lines.append("<b>🧪 Эксперимент на неделю</b>")
    lines.append(_e(analysis["weekly_experiment"]))
    lines.append("")
    lines.append(f"<b>🔮 Прогноз на след. неделю</b>")
    lines.append(_e(analysis["prediction"]))

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


# === Entry point ===

def run() -> None:
    started = datetime.now(timezone.utc)

    # 1. Топ стартапов
    all_startups = fetch_top_startups(limit=100)
    print(f"[trustmrr] получено {len(all_startups)} стартапов", flush=True)

    # 2. Фильтрация
    main, excluded = filter_for_analysis(all_startups)
    print(f"[trustmrr] для анализа: {len(main)}, исключено: {len(excluded)}", flush=True)

    if len(main) < 10:
        raise RuntimeError(
            f"Слишком мало стартапов для анализа: {len(main)} "
            "(меньше 10 после фильтрации)"
        )

    # 3. Grok analysis (берём топ-50 по revenue для качества промпта)
    analysis = call_grok_analysis(main[:50], excluded_count=len(excluded))

    # 4. Render + send
    report = render_report(analysis, total_count=len(main), excluded_count=len(excluded))
    messages = _split_for_telegram(report)
    telegram_sender.send_messages(messages)

    telemetry.write({
        "ts": started.isoformat(),
        "job": "trustmrr_weekly",
        "startups_total": len(all_startups),
        "startups_analyzed": len(main),
        "startups_excluded": len(excluded),
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
                f"❌ trustmrr_weekly упал:\n<pre>{tb[-2500:]}</pre>"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
