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
    "telehealth", "tele-health", "tele health", "online doctor",
    "prescription", "rx ", " rx", "prescribing",
    # Гормоны / репродуктивное здоровье
    "hormone", "hrt ", "testosterone", "fertility clinic",
    # EHR с чувствительными данными
    "ehr ", " ehr", "electronic health record",
    "i/dd", "intellectual and developmental disab",
    "hipaa", "phi data", "patient record",
    # Обход AI-детектеров (риски на стороне образовательных институтов)
    "undetectable", "bypass turnitin", "humanize ai text", "evade detection",
    # Прочие чувствительные категории
    "controlled substance", "kratom", "cannabis prescription",
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
    "=== ЖЁСТКИЕ ПРАВИЛА ===\n\n"
    "1. ИСПОЛЬЗУЙ ТОЛЬКО НАЗВАНИЯ ИЗ ПЕРЕДАННОГО СПИСКА.\n"
    "   Каждое имя компании которое ты упоминаешь в examples/example/tools — "
    "   должно дословно совпадать с полем `name` в одной из переданных записей. "
    "   НЕ выдумывай компании. НЕ дополняй своими знаниями. НЕ называй "
    "   'AEO Engine, Reddit Agency, etc.' если 'Reddit Agency' не было в списке. "
    "   Если в списке нет 3 примеров для какой-то ниши — возьми 2.\n\n"
    "2. ОБЪЯСНЯЙ ДЛЯ ЧЕЛОВЕКА БЕЗ ТЕХ-БЭКГРАУНДА.\n"
    "   Аудитория — предприниматель, который НЕ знает индихакер-жаргон. "
    "   Каждую аббревиатуру и нишевый термин РАСШИФРОВЫВАЙ при первом "
    "   употреблении в скобках простыми словами. Примеры как надо:\n"
    "   - 'AEO (оптимизация под AI-поиск — ChatGPT, Perplexity, Google AI Overview)'\n"
    "   - 'LLM-агенты (программы которые сами выполняют задачи через нейросеть)'\n"
    "   - 'AI outreach (рассылка персонализированных писем клиентам через ИИ)'\n"
    "   - 'churn (% клиентов которые отписываются за месяц)'\n"
    "   - 'CAC (стоимость привлечения одного клиента)'\n"
    "   - 'pSEO (массовая генерация SEO-страниц программой)'\n"
    "   - 'white-label (продаёшь продукт под чужим брендом)'\n"
    "   НЕ употребляй жаргон без расшифровки. Лучше пара слов в скобках "
    "   чем 10 минут гугления для читателя.\n\n"
    "3. ОПИСАНИЯ ДОЛЖНЫ БЫТЬ ЖИТЕЙСКИМИ.\n"
    "   Вместо 'трафик мигрирует с Google в LLM-агенты' пиши "
    "   'люди стали искать ответы в ChatGPT вместо Google, и стартапы помогают "
    "   компаниям попадать в эти ответы'. Конкретные действия, понятные глаголы.\n\n"
    "4. driver / tactic / weekly_experiment — это ДЕЙСТВИЯ, не описания.\n"
    "   'Поисковый трафик мигрирует' — описание, плохо.\n"
    "   'Создай 5 страниц вопрос-ответ → попадёт в ChatGPT-ответы за 2 недели' — действие, хорошо.\n\n"
    "5. Анализируй ВНИМАТЕЛЬНО данные:\n"
    "   - 'Растущие' = высокий MoM growth (>15%), не просто большой MRR\n"
    "   - 'Модель роста' = что общего у тех кто растёт быстро\n"
    "   - 'Паттерны' = повторяющиеся технологические темы\n"
    "   - НЕ пересказывай таблицу — синтезируй insights\n\n"
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
    valid_names = [s["name"] for s in startups]
    return (
        f"Сегодня {datetime.now(_MOSCOW).strftime('%d.%m.%Y')}.\n\n"
        f"Аудитория отчёта — предприниматель который думает про запуск "
        f"маленького SaaS/AI бизнеса. НЕ технарь, индихакер-жаргона не знает. "
        f"Каждый специальный термин обязательно расшифровывай в скобках.\n\n"
        f"Список топ-{len(startups)} стартапов с TrustMRR (отфильтровано: "
        f"{excluded_count} GLP-1/EHR/телемедицина исключены):\n\n"
        f"{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
        f"ВАЛИДНЫЕ ИМЕНА КОМПАНИЙ (используй ТОЛЬКО эти, дословно):\n"
        f"{json.dumps(valid_names, ensure_ascii=False)}\n\n"
        f"Сделай еженедельный аналитический отчёт строго по схеме. "
        f"Перед отправкой ответа проверь: каждое имя в examples/example/tools "
        f"присутствует в списке выше. Если выдумал — замени или удали."
    )


def _validate_names(analysis: dict, valid_names: set[str]) -> tuple[dict, list[str]]:
    """Проверяет что все упомянутые имена есть во входных данных.
    
    Возвращает (analysis, list_of_hallucinated). Не модифицирует — только
    помечает что нашли. Это сигнал для следующего тюнинга промпта.
    """
    hallucinated: list[str] = []
    
    def _check(names_iter):
        for n in names_iter:
            if not n:
                continue
            n_str = str(n).strip()
            if not n_str:
                continue
            # Точное совпадение или подстрока (Grok может слегка переформулировать)
            n_lower = n_str.lower()
            matched = any(
                n_lower == v.lower() or n_lower in v.lower() or v.lower() in n_lower
                for v in valid_names
            )
            if not matched:
                hallucinated.append(n_str)
    
    # top_trends[].examples
    for t in analysis.get("top_trends") or []:
        _check(t.get("examples") or [])
    # high_growth_models[].example — может содержать имя в начале
    for m in analysis.get("high_growth_models") or []:
        ex = m.get("example") or ""
        # Берём первое слово до тире/двоеточия/скобки как кандидата
        first = ex.split("—")[0].split(":")[0].split("(")[0].strip()
        if first and len(first) < 50:
            _check([first])
    # aeo_block.tools
    aeo = analysis.get("aeo_block") or {}
    _check(aeo.get("tools") or [])
    
    return analysis, hallucinated


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
    top_for_analysis = main[:50]
    analysis = call_grok_analysis(top_for_analysis, excluded_count=len(excluded))

    # Anti-hallucination check
    valid_names = {s["name"] for s in top_for_analysis}
    analysis, hallucinated = _validate_names(analysis, valid_names)
    if hallucinated:
        print(f"[trustmrr] ⚠️ Grok упомянул имена которых нет во входных: "
              f"{hallucinated}", flush=True)

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
