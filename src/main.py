"""Entrypoint. Запускается из workflow: `python -m src.main morning`."""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from . import grok_client, dedup, state, render, telegram_sender, telemetry


# ключи всех корзин — для дедупа и сбора хэшей
_ALL_KEYS_NEWS = ("ru_top", "macro")
_BASE_KEYS_FIN = ("crypto", "stocks")  # третий бакет (bigtech/pharma) добавляется в runtime
_ALL_KEYS_THEM = ("sports", "ai")


def _sort_buckets(payload: dict, keys: tuple[str, ...]) -> dict:
    """Сортировка items внутри каждой указанной корзины по score desc."""
    out = dict(payload)
    for k in keys:
        items = payload.get(k, []) or []
        out[k] = sorted(items, key=lambda x: x.get("score", 0), reverse=True)
    return out


def run(slot: str) -> None:
    started = datetime.now(timezone.utc)
    seen = state.load_seen_hashes()

    # 3 вызова Grok, каждый со своей корзиной категорий
    news = grok_client.call_news_digest(seen_hashes=seen)
    financial, third = grok_client.call_financial_digest(seen_hashes=seen)
    thematic = grok_client.call_thematic_digest(seen_hashes=seen)

    fin_keys = _BASE_KEYS_FIN + (third,)

    # дедуп против последних 48ч (точный, по хэшам из state)
    news, dropped_a = dedup.filter_seen(news, seen, keys=_ALL_KEYS_NEWS)
    financial, dropped_b = dedup.filter_seen(financial, seen, keys=fin_keys)
    thematic, dropped_c = dedup.filter_seen(thematic, seen, keys=_ALL_KEYS_THEM)

    # почти-дубли внутри прогона (Jaccard ≥0.8): одна история в двух корзинах
    # с перефразом хэшем не ловится; kept_norms общий на все три payload'а
    _norms: list[str] = []
    news, dropped_d = dedup.filter_near_dups_within(news, _ALL_KEYS_NEWS, _norms)
    financial, dropped_e = dedup.filter_near_dups_within(financial, fin_keys, _norms)
    thematic, dropped_f = dedup.filter_near_dups_within(thematic, _ALL_KEYS_THEM, _norms)

    # сортировка по score desc
    news = _sort_buckets(news, _ALL_KEYS_NEWS)
    financial = _sort_buckets(financial, fin_keys)
    thematic = _sort_buckets(thematic, _ALL_KEYS_THEM)

    # рендер и отправка
    msgs = render.render_digest(
        slot=slot, news=news, financial=financial, thematic=thematic, third=third,
    )
    telegram_sender.send_messages(msgs)

    # обновление state и телеметрии
    new_hashes = dedup.collect_hashes(news, financial, thematic)
    state.merge_and_save(new_hashes)   # старые t сохраняются — окно 48ч реальное
    telemetry.write({
        "ts": started.isoformat(),
        "slot": slot,
        "items_total": len(new_hashes),
        "dropped_dedup": dropped_a + dropped_b + dropped_c,
        "dropped_near_dup": dropped_d + dropped_e + dropped_f,
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
