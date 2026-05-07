"""Локальный sanity-check без отправки в TG.

Запуск:
    XAI_API_KEY=... python -m scripts.dryrun
Печатает JSON от обоих вызовов и финальные сообщения.
"""
from __future__ import annotations

import json
import os
import sys

# Заглушки переменных, чтобы импорты в src/* не упали
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:dryrun")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@dryrun")

from src import grok_client, dedup, state, render  # noqa: E402


def main() -> int:
    seen = state.load_seen_hashes()
    print(f"Loaded {len(seen)} seen hashes", file=sys.stderr)

    news = grok_client.call_news_digest(seen_hashes=seen)
    print("=== NEWS ===")
    print(json.dumps(news, ensure_ascii=False, indent=2))

    verticals = grok_client.call_verticals_digest(seen_hashes=seen)
    print("=== VERTICALS ===")
    print(json.dumps(verticals, ensure_ascii=False, indent=2))

    msgs = render.render_digest(slot="morning", news=news, verticals=verticals)
    print("=== MESSAGES ===")
    for i, m in enumerate(msgs, 1):
        print(f"--- msg {i} ({len(m)} chars) ---")
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
