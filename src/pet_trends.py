"""Субботняя сводка трендов pet-индустрии (косметика/уход за кошками).

Структура из 7 секций по промпту: главный тренд недели, топ-10 трендов,
растущие категории, эко-тренды, цифры и прогнозы 2026-2027, обсуждения
в соцсетях, идеи для мини-бизнеса.

Грузится через те же тулы (x_search + web_search) как и alpha, разница
только в промпте и расписании.
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
    "Ты — аналитик pet-индустрии с фокусом на косметику и уход за кошками. "
    "Готовишь еженедельную сводку для владельца небольшого pet-косметик "
    "бренда в России (бренд Lilloo Laboratory, российский рынок). Опирайся "
    "на свежие данные за последние 30 дней. Используй x_search для трендов "
    "в соцсетях, web_search для рыночных цифр.\n\n"
    "АУДИТОРИЯ: владелец бренда, не маркетолог-аналитик. Хочет за 1 минуту "
    "понять что добавить в линейку и что показывают конкуренты. Не нужен ему "
    "академический обзор индустрии.\n\n"
    "=== ЖЁСТКИЕ ПРАВИЛА ===\n\n"
    "1. РУССКИЙ ЯЗЫК ВЕЗДЕ.\n"
    "   Каждое название, термин и категория — на русском. Английский только "
    "   в скобках при первом упоминании если без него не найти продукт:\n"
    "   - НЕ 'waterless shampoo' — а 'сухой шампунь без смывания'\n"
    "   - НЕ 'dematting comb' — а 'расчёска против колтунов (дематтер)'\n"
    "   - НЕ 'skinification' — а 'кошачий уход по принципу человеческого "
    "     skincare (этапы умывания/тонера/крема для морды и лап)'\n"
    "   - НЕ 'mobile feline-only salons' — а 'выездные груминг-салоны "
    "     только для кошек'\n"
    "   - НЕ 'CAGR 7.56%' без перевода — а 'среднегодовой рост 7,5%'\n\n"
    "2. РФ-ФОКУС.\n"
    "   Минимум 2 из 5 трендов — про российский рынок: Wildberries, Ozon, "
    "   Telegram-каналы зоо-блогеров (русскоязычных), российские бренды "
    "   (PETSHOP.RU, Petface, БК Зоомаг), Авито, тренды русскоязычного "
    "   TikTok про кошек.\n"
    "   Минимум 1 из 4-5 идей для мини-бизнеса — конкретно про российский "
    "   рынок (что добавить в Lilloo, где продавать, на какие маркетплейсы).\n\n"
    "3. РОВНО 5 ТРЕНДОВ, НЕ 10.\n"
    "   И они должны быть РАЗНЫМИ:\n"
    "   - 1 тренд = 1 продукт ИЛИ 1 инструмент ИЛИ 1 услуга ИЛИ 1 канал "
    "     продаж ИЛИ 1 поведение покупателя\n"
    "   - НЕ 3 тренда про шампуни с разных ракурсов\n"
    "   - НЕ дублирование 'эко-составы' и 'натуральные ингредиенты'\n\n"
    "4. ЦИФР МАКСИМУМ 3, КАЖДАЯ С ИСТОЧНИКОМ И ГОДОМ.\n"
    "   Не 5 разных оценок CAGR от разных рисёрч-фирм — это путает читателя. "
    "   Лучше 2-3 крепкие цифры с понятной формулировкой:\n"
    "   - 'Мировой рынок ухода за животными — около $190 млрд в 2026 году "
    "     (Grand View Research)'\n"
    "   - 'Сегмент груминга кошек растёт быстрее собачьего — примерно 8% в "
    "     год (Mordor Intelligence)'\n"
    "   Если точных данных по российскому рынку нет — пиши 'по российскому "
    "   рынку точных данных нет, поэтому опираемся на мировые'. Не выдумывай.\n\n"
    "5. ИДЕИ — КОНКРЕТНО ПОД БРЕНД В РФ.\n"
    "   Не 'cat-specific dematting glove with eco-pack' (это пустая фраза). "
    "   А: 'Добавить в линейку Lilloo сухой шампунь-пенку без смывания — "
    "   удобно для пожилых кошек и тех кто боится воды. Запустить тест 100 "
    "   штук на WB, цена 600-800₽. Конкуренты: Mooncat (мировой), в РФ ниша "
    "   почти пуста — есть шанс зайти первым.'\n"
    "   Каждая идея — что именно сделать, почему именно сейчас, где продавать.\n\n"
    "6. УБРАТЬ СЕКЦИИ-ШУМ.\n"
    "   Из 7 секций оставь 5 (вместо 7):\n"
    "   1) Главный сигнал недели (1-2 предложения)\n"
    "   2) Топ-5 трендов (НЕ 10)\n"
    "   3) Цифры рынка (максимум 3 показателя)\n"
    "   4) Что обсуждают в соцсетях (RU + мир, кратко)\n"
    "   5) 3-4 конкретные идеи для линейки Lilloo\n"
    "   Секции 'Растущие категории' и 'Эко-тренды' — НЕ ОТДЕЛЬНЫЕ. "
    "   Если есть значимое — встрой в Топ-5 трендов как отдельный пункт.\n\n"
    "=== ФОРМАТ HTML ===\n"
    "Только <b>, <i>. Никакого Markdown, никаких ```. Между секциями пустая строка."
)

_USER_TMPL = (
    "Сегодня {date}. Подготовь еженедельную сводку трендов pet-индустрии "
    "(косметика и уход за кошками) за {month_year}. Опирайся на тренды и "
    "обсуждения последних 7-30 дней. Помни: аудитория — владелец маленького "
    "российского бренда косметики для кошек Lilloo Laboratory. Пиши на "
    "русском, без английского жаргона, с РФ-фокусом."
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
    messages = _split_for_telegram(text)
    telegram_sender.send_messages(messages)

    telemetry.write({
        "ts": started.isoformat(),
        "job": "pet_trends_weekly",
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
                max_output_tokens=3000,
                reasoning={"effort": REASONING_EFFORT},
            )
            text = (resp.output_text or "").strip()
            if not text:
                raise RuntimeError("пустой ответ от Grok")
            telemetry.write({
                "job": "pet_trends_weekly",
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
            print(f"[pet_trends] attempt {attempt} on {model} failed: {e!r}, "
                  f"retry in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Режем по двойным переносам — границы секций."""
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
                f"❌ pet_trends_weekly упал:\n<pre>{tb[-2500:]}</pre>"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
