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
    "Ты — охотник за альфой. Твоя задача — находить ранние сигналы на X "
    "(Twitter) и в свежих новостях, которые могут дать асимметричную "
    "доходность тому, кто запустит маленький бизнес или арбитраж первым. "
    "Используй x_search и web_search. Опираешься на сигналы за последнюю "
    "неделю. Никакой воды, общих фраз, налогов и жалоб — только конкретика. "
    "Пиши на русском.\n\n"
    "ФОРМАТ ВЫВОДА — строго HTML для Telegram (теги <b>, <i>, никакого "
    "Markdown, никаких ```code```). Структура:\n\n"
    "Первая строка: <b>🎯 10 прорывных альфа-идей — [месяц год]</b>\n\n"
    "Секция «Главная альфа недели» — 1-2 предложения о самой сильной "
    "возможности.\n\n"
    "Затем нумерованный список из 10 идей. Каждая идея в формате:\n"
    "<b>N. Название идеи</b>\n"
    "<b>Почему сейчас:</b> 1-2 предложения, что именно загорелось на этой "
    "неделе (со ссылкой на конкретный тренд / событие / аккаунт).\n"
    "<b>Как запустить:</b> вход в $, ожидаемая маржа в %, реалистичный срок "
    "до первого дохода (недели/месяцы).\n\n"
    "Между идеями — пустая строка для читаемости.\n\n"
    "ПРАВИЛА:\n"
    "- Только реальные тренды последних 7 дней. Не общие 'AI растёт'.\n"
    "- Низкий-средний вход (до $5k), не венчур.\n"
    "- Edge должен быть конкретный: знание ниши, скорость, локация, "
    "  навык — что именно даёт тебе преимущество.\n"
    "- Можно (но не обязательно) связывать с pet-косметикой.\n"
    "- Не используй <pre>, <code>, длинные тире разделять секции."
)

_USER_TMPL = (
    "Сегодня {date}. Дай мне 10 прорывных альфа-идей с прицелом на "
    "{month_year}. Опирайся на тренды за последние 7 дней."
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
                max_output_tokens=4500,
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
