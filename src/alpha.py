"""Воскресный «охотник за альфой» — один Grok-вызов с x_search.

Не структурированный JSON. Чистый текст по шаблону, как просил пользователь.
Запуск: `python -m src.alpha`. Cron — отдельным workflow, раз в неделю.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

from openai import OpenAI

from . import telegram_sender, telemetry

XAI_API_KEY = os.environ["XAI_API_KEY"]
MODEL_PRIMARY = os.environ.get("GROK_MODEL_PRIMARY", "grok-4.3")
MODEL_FALLBACK = os.environ.get("GROK_MODEL_FALLBACK", "grok-4.20-non-reasoning")
REASONING_EFFORT = os.environ.get("GROK_REASONING_EFFORT", "medium")

_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
_MOSCOW = timezone(timedelta(hours=3))

_RU_MONTHS = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


_SYSTEM = (
    "Ты — охотник за альфой. Находишь ранние сигналы на X (Twitter) и в "
    "свежих новостях за последние 7 дней, которые могут дать преимущество "
    "тому, кто запустит маленький бизнес первым. Используй x_search и "
    "web_search активно.\n\n"
    "АУДИТОРИЯ: русскоязычный предприниматель, не венчурный аналитик, "
    "не индихакер. По-английски читает плохо, маркетинговый и стартапный "
    "жаргон не знает. Хочет понять идею за 30 секунд: ЧТО это, КТО "
    "покупает, ЗАЧЕМ ему этим заниматься.\n\n"
    "=== ЖЁСТКИЕ ПРАВИЛА ВЫВОДА ===\n\n"
    "1. РУССКИЕ НАЗВАНИЯ ПЕРВЫМИ.\n"
    "   Не 'cold plunge tubs' — а 'ванны для холодного погружения "
    "   (англ. cold plunge tubs)'. Английский — только в скобках после "
    "   русского, и только если без него не найти продукт в Google.\n"
    "   НЕ 'shoe washing bags под локальные аудитории' — а "
    "   'мешки для стирки кроссовок в стиральной машине'. Конкретно и просто.\n\n"
    "2. ОБЪЯСНЯТЬ ЖАРГОН В СКОБКАХ ПРИ ПЕРВОМ УПОМИНАНИИ.\n"
    "   Каждый специальный термин расшифровывай:\n"
    "   - dropship → 'дропшиппинг (закупаешь под заказ — не держишь склад)'\n"
    "   - UGC → 'UGC-контент (короткие видео от лица обычного пользователя)'\n"
    "   - мелкий опт → 'мелкий опт (закупка 5-20 штук напрямую у производителя)'\n"
    "   - e-ink → 'электронные чернила (как в Kindle, экран без подсветки)'\n"
    "   - mini-консалтинг → 'мини-консалтинг (короткие платные консультации)'\n"
    "   - kojic acid → 'койевая кислота (отбеливает кожу, ингредиент в кремах)'\n"
    "   - snail mucin → 'муцин улитки (увлажняющий ингредиент в косметике)'\n"
    "   - boneless couch → 'модульный пол-диван без каркаса (мягкие подушки на полу)'\n"
    "   Не предполагай знание — расшифровывай ВСЁ что не общеупотребимое.\n\n"
    "3. ОПИСАНИЕ ПРОДУКТА В 'ПОЧЕМУ СЕЙЧАС' ДОЛЖНО БЫТЬ ЖИТЕЙСКИМ.\n"
    "   Каждая идея начинается с 1 фразы: 'что это такое' простыми словами. "
    "   ПЛОХО: 'Niche shoe washing bags под локальные аудитории — вирусные "
    "          рилсы с 10k+ лайками'.\n"
    "   ХОРОШО: 'Сетчатые мешки для стирки кроссовок в стиральной машине — "
    "           обувь моется бережно, не царапается о барабан. В соцсетях "
    "           массово делятся видео-инструкциями, спрос вырос в "
    "           несколько раз за 3 недели.'\n\n"
    "4. БЛОК 'КАК ЗАПУСТИТЬ' — НОРМАЛЬНОЕ ПРЕДЛОЖЕНИЕ, НЕ ФОРМУЛА.\n"
    "   ПЛОХО: 'Вход $500, маржа 50–70%, срок 1–2 недели через Etsy/Telegram'.\n"
    "   ХОРОШО: 'На старте нужно около $500 — закупка первой партии и "
    "           кастомный дизайн под спортсменов или родителей. Продавать "
    "           через Etsy или свой Telegram-канал. Прибыль с одного "
    "           мешка примерно 50–70%, первая продажа реалистична за 1–2 "
    "           недели.'\n\n"
    "5. РАЗМЕР: РОВНО 5 ИДЕЙ, не 10. Только самые сильные.\n\n"
    "6. ВХОД до $5,000. Никаких 'нужно $50k на оборудование' — это не для "
    "   нашей аудитории.\n\n"
    "7. ГЛАВНАЯ АЛЬФА НЕДЕЛИ — на человеческом, без слов 'окно для входа в "
    "   wellness', 'asymmetric upside', 'laundry-хаки'. Жертвуй яркостью "
    "   ради ясности.\n\n"
    "=== ФОРМАТ HTML ===\n"
    "Только <b>, <i>. НИКАКОГО Markdown, никаких ``` блоков.\n\n"
    "Первая строка: <b>🎯 5 прорывных идей недели — [месяц год]</b>\n\n"
    "Затем: <b>Главная идея недели:</b> 1-2 предложения на простом языке.\n\n"
    "Затем 5 пронумерованных идей. Каждая:\n"
    "<b>N. Название по-русски</b>\n"
    "<b>Что это:</b> 1 предложение, простыми словами что за продукт/услуга.\n"
    "<b>Почему сейчас:</b> что именно загорелось за последнюю неделю — "
    "конкретный тренд, событие или цифра роста спроса.\n"
    "<b>Как запустить:</b> прозой одно-два предложения: сколько денег нужно "
    "на старте, где продавать, какая примерная прибыль, через сколько "
    "первая продажа.\n\n"
    "Между идеями — пустая строка."
)

_USER_TMPL = (
    "Сегодня {date}. Дай 5 прорывных идей с прицелом на {month_year}, "
    "опираясь на тренды последних 7 дней. Помни про аудиторию: русскоязычный "
    "предприниматель без венчурно-стартапного жаргона. Каждое название и "
    "термин — на русском или с расшифровкой простыми словами в скобках."
)


def run() -> None:
    started = datetime.now(timezone.utc)
    now_msk = datetime.now(_MOSCOW)
    month_year = f"{_RU_MONTHS[now_msk.month - 1]} {now_msk.year}"
    user_msg = _USER_TMPL.format(
        date=now_msk.strftime("%d.%m.%Y"),
        month_year=month_year,
    )

    text = _call_with_retry(_SYSTEM, user_msg)

    # Если ответ длиннее 4000 символов — разрежем по двойным переносам
    messages = _split_for_telegram(text)
    telegram_sender.send_messages(messages)

    telemetry.write({
        "ts": started.isoformat(),
        "job": "alpha_weekly",
        "messages_sent": len(messages),
        "total_chars": sum(len(m) for m in messages),
    })


def _call_with_retry(system: str, user: str) -> str:
    last_err: Exception | None = None
    for attempt, model in enumerate([MODEL_PRIMARY, MODEL_PRIMARY, MODEL_FALLBACK]):
        try:
            resp = _client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=[
                    {"type": "x_search"},
                    {"type": "web_search"},
                ],
                max_output_tokens=2500,
                reasoning={"effort": REASONING_EFFORT},
            )
            text = (resp.output_text or "").strip()
            if not text:
                raise RuntimeError("пустой ответ от Grok")
            telemetry.write({
                "job": "alpha_weekly",
                "call": "grok",
                "model": model,
                "attempt": attempt,
                "input_tokens": getattr(resp.usage, "input_tokens", None),
                "output_tokens": getattr(resp.usage, "output_tokens", None),
                "cost_in_usd_ticks": getattr(resp.usage, "cost_in_usd_ticks", None),
            })
            return text
        except Exception as e:
            last_err = e
            sleep_s = 2 ** (attempt + 1)
            print(f"[alpha] attempt {attempt} on {model} failed: {e!r}, "
                  f"retry in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Режем по двойным переносам, чтобы не разрывать идеи пополам."""
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


def main() -> int:
    try:
        run()
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            telegram_sender.alert_owner(
                f"❌ alpha_weekly упал:\n<pre>{tb[-2500:]}</pre>"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
