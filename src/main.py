"""Entrypoint. Запускается из workflow: `python -m src.main morning`."""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from . import grok_client, dedup, state, render, telegram_sender, telemetry


# ключи всех корзин — для дедупа и сбора хэшей
_ALL_KEYS_NEWS = ("ru_top", "macro")
_ALL_KEYS_FIN = ("crypto", "stocks", "bigtech")
_ALL_KEYS_THEM = ("sports", "ai")


def run(slot: str) -> None:
    started = datetime.now(timezone.utc)
    seen = state.load_seen_hashes()

    # 3 вызова Grok, каждый со своей корзиной категорий
    news = grok_client.call_news_digest(seen_hashes=seen)
    financial = grok_client.call_financial_digest(seen_hashes=seen)
    thematic = grok_client.call_thematic_digest(seen_hashes=seen)

    # дедуп против последних 48ч
    news, dropped_a = dedup.filter_seen(news, seen, keys=_ALL_KEYS_NEWS)
    financial, dropped_b = dedup.filter_seen(financial, seen, keys=_ALL_KEYS_FIN)
    thematic, dropped_c = dedup.filter_seen(thematic, seen, keys=_ALL_KEYS_THEM)

    # рендер и отправка
    msgs = render.render_digest(
        slot=slot, news=news, financial=financial, thematic=thematic,
    )
    telegram_sender.send_messages(msgs)

    # обновление state и телеметрии
    new_hashes = dedup.collect_hashes(news, financial, thematic)
    state.save_seen_hashes(seen + new_hashes)
    telemetry.write({
        "ts": started.isoformat(),
        "slot": slot,
        "items_total": len(new_hashes),
        "dropped_dedup": dropped_a + dropped_b + dropped_c,
        "messages_sent": len(msgs),
    })


def main() -> int:
    slot = sys.argv[1] if len(sys.argv) > 1 else "morning"
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


if __name__ == "__main__":
    sys.exit(main())
