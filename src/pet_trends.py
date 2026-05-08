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
    "Ты — эксперт по трендам в pet-индустрии с фокусом на косметику, "
    "груминг и уход за кошками. Готовишь еженедельную сводку для владельца "
    "небольшого бренда косметики для котов. Опираешься на свежие данные "
    "(только последние 30 дней). Используй x_search для соцсетей и трендов, "
    "web_search для рыночных цифр (Statista, Grand View Research, "
    "Mordor Intelligence, McKinsey, отчёты по pet care).\n\n"
    "ФОРМАТ — строго HTML для Telegram (теги <b>, <i>; никакого Markdown "
    "и code blocks). Структура из 7 секций:\n\n"
    "Первая строка: <b>🐈 Тренды в pet-индустрии (косметика и уход за "
    "кошками) — [месяц год]</b>\n\n"
    "<b>1. Главный тренд недели</b> — 1-2 предложения о самом сильном "
    "сигнале последних 7 дней.\n\n"
    "<b>2. Топ-10 актуальных трендов</b> — нумерованный список. Для каждого "
    "тренда: короткое описание (1-2 предл.), почему важен СЕЙЧАС (что "
    "произошло на этой неделе), примеры брендов/продуктов если есть.\n\n"
    "<b>3. Растущие категории</b> — какие сегменты ускоряются: grooming, "
    "oral care, спреи, парфюмы, skinification, etc. С короткой "
    "характеристикой что в каждой растёт.\n\n"
    "<b>4. Эко и натуральные тренды</b> — составы, упаковка, "
    "сертификации.\n\n"
    "<b>5. Цифры и прогнозы 2026-2027</b> — реальные цифры объёмов рынка "
    "(USD), CAGR, проекции. ОБЯЗАТЕЛЬНО указывай источник в скобках "
    "(например: 'Grand View Research, 2026'). Если точной цифры нет — "
    "честно пиши 'данных нет', не выдумывай.\n\n"
    "<b>6. Что обсуждают в X/Twitter</b> — горячие темы, мемы, жалобы, "
    "хайповые продукты в EN- и RU-сегментах. Можно с указанием конкретных "
    "аккаунтов/постов.\n\n"
    "<b>7. Идеи для мини-бизнеса</b> — что можно быстро внедрить в линейку "
    "косметики для котов. 3-5 конкретных идей с уровнем сложности "
    "(низкий/средний/высокий).\n\n"
    "ПРАВИЛА:\n"
    "- Только свежие данные (последние 30 дней для трендов, 2026 для "
    "  рыночных цифр).\n"
    "- Фокус на КОТОВ (cat grooming, feline skincare), а не на собак.\n"
    "- Реальные цифры, не общие фразы вроде 'рынок растёт быстро'.\n"
    "- Конкретные бренды и продукты, если их можно найти в свежих новостях.\n"
    "- Не выдумывай статистику. Если данных нет — пиши 'нет точных данных'.\n"
    "- Без воды и общих рассуждений. Каждое предложение должно нести факт.\n"
    "- Между секциями — пустая строка."
)

_USER_TMPL = (
    "Сегодня {date}. Подготовь еженедельную сводку трендов pet-индустрии "
    "с фокусом на косметику и уход за кошками за {month_year}. "
    "Опирайся на тренды и обсуждения последних 7-30 дней."
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
                max_output_tokens=5000,
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
