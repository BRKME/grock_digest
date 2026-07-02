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
    """Удалить элементы, чей хэш уже в seen_hashes (точное совпадение).

    Почти-дубли по Jaccard — отдельно, в filter_near_dups_within (внутри
    прогона): против истории Jaccard невозможен, state хранит только хэши."""
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


def filter_near_dups_within(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    kept_norms: list[str],
    threshold: float = 0.8,
) -> tuple[dict[str, Any], int]:
    """Почти-дубли ВНУТРИ одного прогона (Jaccard ≥ threshold по словам).

    Одна история часто приходит в две корзины с перефразом («ЦБ снизил ставку
    до 12%» в ru_top и stocks) — хэш-дедуп это не ловит. kept_norms — общий
    накопитель нормализованных тайтлов между вызовами (news -> financial ->
    thematic), чтобы ловить дубли и между payload'ами.

    Против ИСТОРИИ (прошлые прогоны) так нельзя: state хранит только хэши,
    текстов нет — там дедуп остаётся точным по хэшу.
    """
    dropped = 0
    out = dict(payload)
    for k in keys:
        items = payload.get(k, []) or []
        kept = []
        for item in items:
            norm = normalize(item.get("title", ""))
            if any(_jaccard(norm, prev) >= threshold for prev in kept_norms):
                dropped += 1
                continue
            kept_norms.append(norm)
            kept.append(item)
        out[k] = kept
    return out, dropped
