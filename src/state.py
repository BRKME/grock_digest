"""Простой state.json в корне репо. Хранит хэши тайтлов за последние 48ч.

Файл коммитится обратно в репо после успешного запуска (в workflow).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"
WINDOW_SECONDS = 48 * 3600


def load_seen_hashes() -> list[str]:
    if not STATE_PATH.exists():
        return []
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    now = int(time.time())
    fresh = [
        rec["h"] for rec in data.get("seen", [])
        if isinstance(rec, dict) and now - rec.get("t", 0) < WINDOW_SECONDS
    ]
    return fresh


def save_seen_hashes(all_hashes: list[str]) -> None:
    """Persist `all_hashes` deduplicated, with current ts on every write."""
    now = int(time.time())
    seen_set = list(dict.fromkeys(all_hashes))  # preserve order, drop dups
    payload = {
        "seen": [{"h": h, "t": now} for h in seen_set],
        "updated_at": now,
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
