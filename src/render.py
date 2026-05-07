"""Jinja2 рендер дайджеста в MarkdownV2 + сплит на 2 сообщения для Telegram."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_MOSCOW = timezone(timedelta(hours=3))

# MarkdownV2 спецсимволы которые нужно эскейпить
_MD2_ESCAPE = re.compile(r"([_\*\[\]\(\)~`>#\+\-=\|\{\}\.!\\])")


def md2(s: str) -> str:
    if s is None:
        return ""
    return _MD2_ESCAPE.sub(r"\\\1", str(s))


_RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _ru_date(dt: datetime) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month - 1]} {dt.year}"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(disabled_extensions=("md", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md2"] = md2
    return env


def render_digest(*, slot: str, news: dict[str, Any], verticals: dict[str, Any]) -> list[str]:
    """Возвращает список сообщений (≤4000 символов каждое) для последовательной отправки."""
    env = _env()
    now = datetime.now(_MOSCOW)
    ctx = {
        "slot": slot,
        "slot_label": "Утренний" if slot == "morning" else "Вечерний",
        "slot_emoji": "🌅" if slot == "morning" else "🌇",
        "date_str": _ru_date(now),
        "time_str": now.strftime("%H:%M MSK"),
        "news": news,
        "verticals": verticals,
    }
    msg1 = env.get_template("digest_part1.md.j2").render(**ctx)
    msg2 = env.get_template("digest_part2.md.j2").render(**ctx)
    return [msg1.strip(), msg2.strip()]
