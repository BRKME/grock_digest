"""Entrypoint. Запускается из workflow: `python -m src.main morning|evening`."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone

from . import grok_client, dedup, state, render, telegram_sender, telemetry


def run(slot: str) -> None:
    started = datetime.now(timezone.utc)
    seen = state.load_seen_hashes()

    # Call A — общие новости EN+RU в одном вызове
    news = grok_client.call_news_digest(seen_hashes=seen)
    # Call B — 4 вертикали
    verticals = grok_client.call_verticals_digest(seen_hashes=seen)

    # Дедуп против последних 48ч
    news, dropped_a = dedup.filter_seen(news, seen, keys=("en_top", "ru_top"))
    verticals, dropped_b = dedup.filter_seen(
        verticals, seen, keys=("crypto", "stocks", "sports", "ai")
    )

    # Рендер MarkdownV2 + отправка
    msgs = render.render_digest(slot=slot, news=news, verticals=verticals)
    telegram_sender.send_messages(msgs)

    # Обновление состояния и телеметрии
    new_hashes = dedup.collect_hashes(news, verticals)
    state.save_seen_hashes(seen + new_hashes)
    telemetry.write({
        "ts": started.isoformat(),
        "slot": slot,
        "items_total": len(new_hashes),
        "dropped_dedup": dropped_a + dropped_b,
    })


def main() -> int:
    slot = sys.argv[1] if len(sys.argv) > 1 else _slot_by_hour()
    if slot not in ("morning", "evening"):
        print(f"Bad slot: {slot}", file=sys.stderr)
        return 2
    try:
        run(slot)
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            telegram_sender.alert_owner(
                f"❌ grock_digest [{slot}] упал:\n<pre>{tb[-2500:]}</pre>"
            )
        except Exception:
            pass
        return 1


def _slot_by_hour() -> str:
    h = datetime.now(timezone.utc).hour
    return "morning" if h < 12 else "evening"


if __name__ == "__main__":
    sys.exit(main())
