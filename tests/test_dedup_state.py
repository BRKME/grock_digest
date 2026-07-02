"""Smoke-тесты digest: дедуп (хэш + почти-дубли) и 48ч-окно state.
Написаны 02.07.2026 после аудита: _jaccard был мёртвым кодом (почти-дубли
проходили), а save штамповал ВСЕ хэши свежим t=now — окно 48ч не истекало."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dedup


def test_normalize_stable():
    assert dedup.normalize("Bitcoin — UP, 10%!") == dedup.normalize("bitcoin up 10")


def test_exact_dup_dropped():
    seen = [dedup.title_hash("Bitcoin drops below $60k")]
    payload = {"crypto": [{"title": "Bitcoin drops below $60k"},
                          {"title": "ETH steady"}]}
    out, dropped = dedup.filter_seen(payload, seen, keys=("crypto",))
    assert dropped == 1 and len(out["crypto"]) == 1


def test_near_dup_within_run_dropped():
    """Одна история в двух корзинах с перефразом — второй экземпляр выпадает."""
    news = {"ru_top": [{"title": "ЦБ снизил ключевую ставку до 12 процентов"}]}
    fin = {"stocks": [{"title": "ЦБ снизил ключевую ставку до 12"}]}
    kept = []
    n2, d1 = dedup.filter_near_dups_within(news, ("ru_top",), kept)
    f2, d2 = dedup.filter_near_dups_within(fin, ("stocks",), kept)
    assert d1 == 0 and d2 == 1 and len(f2["stocks"]) == 0


def test_different_stories_survive_near_dup():
    p = {"ai": [{"title": "OpenAI выпустила новую модель"},
                {"title": "Nvidia отчиталась о рекордной выручке"}]}
    out, dropped = dedup.filter_near_dups_within(p, ("ai",), [])
    assert dropped == 0 and len(out["ai"]) == 2


def test_state_window_expires(tmp_path, monkeypatch):
    """Старый хэш обязан ИСТЕЧЬ через 48ч, даже если save вызывался вчера."""
    from src import state
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "state.json")
    t0 = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: t0)
    state.merge_and_save(["hash_old"])
    # +30ч: hash_old ещё жив, добавляем новый
    monkeypatch.setattr(time, "time", lambda: t0 + 30 * 3600)
    assert "hash_old" in state.load_seen_hashes()
    state.merge_and_save(["hash_new"])
    # +50ч от t0: old обязан истечь (у него ОРИГИНАЛЬНЫЙ t, не обновлённый)
    monkeypatch.setattr(time, "time", lambda: t0 + 50 * 3600)
    fresh = state.load_seen_hashes()
    assert "hash_old" not in fresh, "t старого хэша обновился при save — окно фейковое"
    assert "hash_new" in fresh
