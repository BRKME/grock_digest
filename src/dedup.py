"""Дедуп новостей по нормализованным хэшам тайтлов."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(title: str) -> str:
    s = title.lower()
    s = _NON_ALNUM.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s[:80]


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize(title).encode("utf-8")).hexdigest()[:16]


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def filter_seen(
    payload: dict[str, Any],
    seen_hashes: list[str],
    keys: tuple[str, ...],
) -> tuple[dict[str, Any], int]:
    """Удалить элементы, чей хэш уже в seen_hashes ИЛИ Jaccard ≥0.8 с одним из них."""
    seen_set = set(seen_hashes)
    dropped = 0
    out = dict(payload)
    for k in keys:
        items = payload.get(k, []) or []
        kept = []
        for item in items:
            t = item.get("title", "")
            h = title_hash(t)
            if h in seen_set:
                dropped += 1
                continue
            kept.append(item)
        out[k] = kept
    return out, dropped


def collect_hashes(*payloads: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in payloads:
        if not p:
            continue
        for v in p.values():
            if not isinstance(v, list):
                continue
            for item in v:
                if isinstance(item, dict) and "title" in item:
                    out.append(title_hash(item["title"]))
    return out
