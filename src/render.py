"""Jinja2 рендер дайджеста в Telegram HTML mode + сплит на сообщения.

Структура: 4 сообщения.
1. header + Топ RU
2. Macro + Crypto
3. Stocks + Big Tech
4. Sports + AI
"""
from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_MOSCOW = timezone(timedelta(hours=3))
_MAX_MSG = 3800  # запас от лимита 4096


def e(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def url(s: Any) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace('"', "&quot;")


_RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _ru_date(dt: datetime) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month - 1]} {dt.year}"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(disabled_extensions=("html", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["e"] = e
    env.filters["url"] = url
    return env


def render_digest(
    *,
    slot: str,
    news: dict[str, Any],
    financial: dict[str, Any],
    thematic: dict[str, Any],
) -> list[str]:
    env = _env()
    now = datetime.now(_MOSCOW)
    # Все корзины в один словарь — шаблоны обращаются как buckets.crypto и т.п.
    buckets = {**(news or {}), **(financial or {}), **(thematic or {})}
    base_ctx = {
        "slot": slot,
        "slot_label": "Утренний",  # сейчас всегда утро
        "slot_emoji": "🌅",
        "date_str": _ru_date(now),
        "time_str": now.strftime("%H:%M MSK"),
        "buckets": buckets,
    }
    parts: list[str] = []
    for tpl in (
        "digest_part1.html.j2",   # header + ru_top
        "digest_part2.html.j2",   # macro + crypto
        "digest_part3.html.j2",   # stocks + bigtech
        "digest_part4.html.j2",   # sports + ai
    ):
        msg = env.get_template(tpl).render(**base_ctx).strip()
        if not msg:
            continue
        if len(msg) <= _MAX_MSG:
            parts.append(msg)
        else:
            parts.extend(_safe_split(msg))
    return parts


def _safe_split(text: str) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        candidate = (cur + "\n\n" + block) if cur else block
        if len(candidate) > _MAX_MSG and cur:
            chunks.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks
