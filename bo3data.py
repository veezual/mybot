"""
bo3data.py — V106: быстрый источник данных bo3.gg (HTTP JSON, БЕЗ браузера).

Открытый API сайта bo3.gg (CS2-статистика). Ключи не нужны, Cloudflare
не душит простые запросы, ответ за 0.5–2.5 секунды. Заменяет Camoufox/HLTV
в трёх ролях:

  • винрейты карт команды — из истории матчей (6 месяцев, до 50 матчей,
    каждая карта: de_anubis -> "Anubis", победитель каждой карты известен);
  • личные встречи (H2H) — фильтр "матчи с обеими командами";
  • поиск команды по имени — filter[teams.name][like] + нечёткое сравнение.

Все пары "имя HLTV -> bo3-id" кэшируются в базе (entity_map), повторные
клики мгновенны. Любая ошибка -> исключение, collector откатится на
старый путь (Camoufox), бот не падает.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import aiohttp

import config
import database as db
from odds_api import norm_name

try:
    from rapidfuzz import fuzz
    def fuzzy_ratio(a: str, b: str) -> float:
        return float(fuzz.token_sort_ratio(a, b))
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher
    def fuzzy_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100

log = logging.getLogger("bo3data")

BO3_BASE = "https://api.bo3.gg"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://bo3.gg/",
}
TIMEOUT = aiohttp.ClientTimeout(total=25)

# Кэш имён в памяти: norm_name(имя) -> bo3 team dict или None
_team_cache: dict = {}
# чтобы не долбить API параллельными поисками одной и той же команды
_search_locks: dict[str, asyncio.Lock] = {}

# de_dust2 -> Dust2 и т.п. (стиль HLTV, чтобы прогноз сравнивал корректно)
_MAP_SPECIAL = {
    "de_dust2": "Dust2", "de_dust": "Dust2", "de_train": "Train",
    "de_mills": "Mills", "de_grail": "Grail", "de_palais": "Palais",
    "de_abyss": "Abyss", "de_breach": "Breach", "de_bugatti": "Bugatti",
}


def norm_map(raw: str) -> str:
    """de_anubis -> Anubis (в стиле HLTV)."""
    m = (raw or "").strip().lower()
    if not m:
        return ""
    if m in _MAP_SPECIAL:
        return _MAP_SPECIAL[m]
    if m.startswith("de_"):
        m = m[3:]
    return m.replace("_", " ").strip().capitalize()


# ---------------------------------------------------------------- HTTP

async def _get(path: str, params: dict | None = None):
    url = f"{BO3_BASE}{path}"
    if params:
        url += "?" + urlencode(params, safe="[],=")
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"bo3.gg {path} -> HTTP {r.status}")
            return await r.json(content_type=None)


# ---------------------------------------------------------------- поиск команды

_ACADEMY_WORDS = ("academy", "junior", "youth", " fe ", "women", "talent",
                  "school", " next ", " nxt ", " young ", " pro ")


def _miss_key(name: str) -> str:
    return f"bo3_miss:{norm_name(name)}"


def _miss_fresh(name: str) -> bool:
    """V114: имя недавно не нашлось — не долбим API (отрицательный кэш 24 ч).
    V115: сюда попадает ТОЛЬКО честное «bo3.gg не знает такого имени»;
    сетевые сбои живут в отдельном коротком кэше (_net_*)."""
    try:
        raw = db.get_meta(_miss_key(name), "")
        return bool(raw) and (time.time() - float(raw)) < config.BO3_MISS_TTL
    except Exception:
        return False


def _miss_mark(name: str):
    try:
        db.set_meta(_miss_key(name), str(time.time()))
    except Exception:
        pass


# ---- V115: отдельный короткий кэш СЕТЕВЫХ сбоев (не путать с «не найдена») ----

def _net_key(name: str) -> str:
    return f"bo3_net:{norm_name(name)}"


def _net_fresh(name: str) -> bool:
    """V115: недавно был таймаут/HTTP-сбой — подождём BO3_NET_TTL (15 мин),
    но НЕ клеймим команду отсутствующей (та ошибка жила сутки)."""
    try:
        raw = db.get_meta(_net_key(name), "")
        return bool(raw) and (time.time() - float(raw)) < config.BO3_NET_TTL
    except Exception:
        return False


def _net_mark(name: str):
    try:
        db.set_meta(_net_key(name), str(time.time()))
    except Exception:
        pass


def _net_clear(name: str):
    try:
        db.set_meta(_net_key(name), "0")
    except Exception:
        pass


def _miss_clear(name: str):
    try:
        db.set_meta(_miss_key(name), "0")
    except Exception:
        pass


def _cache_save(name: str, team: dict):
    """V114: кэш v2 — id|slug|бо3-имя. Бо3-имя нужно для диагностики:
    видно, КОГО мы привязали (Imperial Pro vs Imperial)."""
    _team_cache[norm_name(name)] = team
    try:
        db.set_meta(f"bo3_team:{norm_name(name)}",
                    f"{team['id']}|{team.get('slug', '')}|{team.get('name', '')}")
    except Exception:
        pass


def _cache_load(name: str) -> dict | None:
    key = norm_name(name)
    if key in _team_cache:
        t = _team_cache[key]
        return dict(t) if t else None
    try:
        saved = db.get_meta(f"bo3_team:{key}", "")
    except Exception:
        return None
    if not saved or "|" not in saved:
        return None
    parts = saved.split("|", 2)
    bid = parts[0]
    slug = parts[1] if len(parts) > 1 else ""
    bname = parts[2] if len(parts) > 2 else name
    if not bid:
        return None
    t = {"id": bid, "slug": slug, "name": bname or name, "logo": ""}
    _team_cache[key] = t
    return t


def _score_candidate(name: str, t: dict) -> float:
    """V114: точное имя > точный слаг > нечёткое; академии/молодёжки вниз,
    если в запросе их нет ('Imperial' не должен биться с 'Imperial Pro')."""
    key = norm_name(name)
    tn = norm_name(t.get("name") or "")
    slug = norm_name((t.get("slug") or "").replace("-", " "))
    s = fuzzy_ratio(key, tn)
    if tn == key:
        s += 100
    if slug and slug == key:
        s = max(s, 90.0) + 90
    s = max(s, fuzzy_ratio(key, slug) * 0.95)
    low = f" {(t.get('slug') or '').lower()} {(t.get('name') or '').lower()} "
    if any(w in low for w in _ACADEMY_WORDS) and not any(w in f" {key} " for w in _ACADEMY_WORDS):
        s -= 60
    return s


async def find_team(name: str, fresh: bool = False,
                    validate: bool = False) -> dict | None:
    """
    Ищет команду bo3.gg по названию (как в HLTV/БК).
    Возврат: {id, slug, name, logo} либо None.

    V114:
      • fresh=True — игнорировать кэш (самолечение после пустой истории);
      • validate=True — кандидат проверяется ИСТОРИЕЙ: если у лучшего
        кандидата матчей нет, пробуем следующий (Imperial Pro умирает,
        живой Imperial находится); до 3 попыток.
      • имя, которое не нашлось, кэшируется на BO3_MISS_TTL (24 ч).
    """
    key = norm_name(name)
    if not fresh:
        cached = _cache_load(name)
        if cached:
            return cached
        if _miss_fresh(name) or _net_fresh(name):
            return None

    lock = _search_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not fresh:
            cached = _cache_load(name)      # пока ждали лок — нашёл сосед
            if cached:
                return cached
            if _miss_fresh(name) or _net_fresh(name):
                return None
        try:
            data = await _get("/api/v1/teams", {
                "filter[teams.name][like]": name,
                "page[limit]": 15,
            })
        except Exception as e:
            # V115: таймаут/429/сбой сети — НЕ «команда не найдена».
            # Раньше здесь ставился 24-часовой miss, и команда сутки
            # выпадала из прогнозов после одного сетевого залапа.
            log.warning("поиск команды bo3 '%s': %s (сетевой сбой, кулдаун %s мин)",
                        name, e, config.BO3_NET_TTL // 60)
            _net_mark(name)
            _team_cache[key] = None
            return None
        rows = data.get("results") or []
        if not rows:
            _miss_mark(name)
            _team_cache[key] = None
            return None
        _net_clear(name)   # V115: API жив — сетевой кулдаун снимаем

        rows.sort(key=lambda t: _score_candidate(name, t), reverse=True)

        def _mk(t: dict) -> dict:
            return {"id": str(t["id"]), "slug": t.get("slug") or "",
                    "name": t.get("name") or name,
                    "logo": t.get("image_url") or ""}

        best = _mk(rows[0])
        if not validate:
            _cache_save(name, best)
            _miss_clear(name)
            return best

        # validate: кандидат обязан быть ЖИВЫМ — иметь хотя бы один
        # завершённый матч за полгода (Imperial Pro/старые теги отсеиваются)
        for cand in rows[:3]:
            c = _mk(cand)
            try:
                hist = await team_history(c, months=6, limit=15)
            except Exception:
                hist = []
            if hist:
                _cache_save(name, c)
                _miss_clear(name)
                return c
            log.info("bo3: кандидат '%s' (id %s) без живой истории — пробую следующего",
                     c.get("name"), c.get("id"))
        _miss_mark(name)
        _team_cache[key] = None
        raise RuntimeError(f"нет карт в истории bo3 для '{name}'")


async def resolve_team(name: str) -> dict | None:
    """Публичный алиас: найти команду (сначала кэш базы, потом API)."""
    return await _team_by_id_fast(name)


async def _team_by_id_fast(name: str) -> dict | None:
    """Сначала кэш в базе, потом API."""
    return _cache_load(name) or await find_team(name)


# ---------------------------------------------------------------- история/карты

async def team_history(team: dict, months: int = 6, limit: int = 50) -> list[dict]:
    """Завершённые матчи команды с играми (картами)."""
    since = (datetime.utcnow() - timedelta(days=30 * months)).strftime("%Y-%m-%d")
    now = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    data = await _get("/api/v1/matches", {
        "page[offset]": 0, "page[limit]": limit,
        "sort": "-start_date",
        "filter[matches.status][in]": "finished",
        "filter[matches.team_ids][overlap]": team["id"],
        "filter[matches.start_date][lt]": now,
        "filter[matches.start_date][gt]": since,
        "filter[matches.discipline_id][eq]": 1,
        "with": "teams,tournament,games",
    })
    return data.get("results") or []


def maps_from_history(matches: list[dict], team_id: str, return_games: bool = False):
    """Винрейты карт из истории: {карта: процент} (как в team_maps).

    V107: карты с малой выборкой (< config.MIN_MAP_GAMES матчей) выбрасываются —
    "Cache 100%" после одной игры качал модель наравне с картой на 30 матчей.
    Защита: если после фильтра пусто (новый ростер/короткая история) —
    возвращаем без фильтра, чтобы не остаться совсем без карт.
    V109: return_games=True -> ({карта: %}, {карта: число игр в выборке}) —
    числа игр нужны предикту для байесовского шринкейджа к 50%.
    """
    wins: dict[str, int] = {}
    total: dict[str, int] = {}
    for m in matches:
        for g in (m.get("games") or []):
            mp = norm_map(g.get("map_name") or "")
            if not mp:
                continue
            w = ((g.get("winner_team_clan") or {}).get("team") or {}).get("id")
            total[mp] = total.get(mp, 0) + 1
            if str(w) == str(team_id):
                wins[mp] = wins.get(mp, 0) + 1
    all_wr = {mp: round(100.0 * wins.get(mp, 0) / n, 1)
              for mp, n in total.items() if n}
    out = {mp: wr for mp, wr in all_wr.items()
           if total[mp] >= config.MIN_MAP_GAMES}
    final = dict(sorted((out or all_wr).items(), key=lambda x: -x[1]))
    if return_games:
        games = {mp: int(total[mp]) for mp in final if mp in total}
        return final, games
    return final


async def history_maps(team: dict, months: int = 6, limit: int = 50) -> dict[str, float]:
    """Винрейты карт для УЖЕ НАЙДЕННОЙ команды (без поиска)."""
    hist = await team_history(team, months=months, limit=limit)
    maps = maps_from_history(hist, team["id"])
    if not maps:
        raise RuntimeError(f"нет карт в истории bo3 для '{team['name']}'")
    return maps


def _side_id(m: dict, key: str, idx: int) -> str | None:
    """V107: id стороны матча в bo3.gg без предположений о формате.

    Поле бывает dict ({"id": ...}), вложенным ({"team": {"id": ...}})
    или отсутствовать — тогда берём teams[idx] из общего списка.
    """
    v = m.get(key)
    if isinstance(v, dict):
        inner = v.get("team") if isinstance(v.get("team"), dict) else v
        sid = inner.get("id")
        if sid is not None:
            return str(sid)
    teams = m.get("teams")
    if isinstance(teams, (list, tuple)) and len(teams) > idx:
        t = teams[idx]
        if isinstance(t, dict):
            if isinstance(t.get("team"), dict):
                t = t["team"]
            if t.get("id") is not None:
                return str(t["id"])
    return None


def form_from_history(matches: list[dict], team_id: str, n: int = 10) -> dict:
    """
    Форма команды: победы в последних n матчах + текущая серия.
    matches — история, отсортированная от свежих к старым (sort=-start_date).
    Возврат: {wins, total, streak, streak_won, last: [1/0, ...]}.
    V107: id команды берётся защитно (_side_id) — раньше, если bo3.gg отвечал
    вложенной структурой, форма молча возвращалась пустой и выпадала из модели.
    """
    tid = str(team_id)
    results: list[int] = []
    for m in matches:
        a_id = _side_id(m, "team1", 0)
        b_id = _side_id(m, "team2", 1)
        s1, s2 = m.get("team1_score"), m.get("team2_score")
        if s1 is None or s2 is None:
            w1 = w2 = 0
            for g in (m.get("games") or []):
                w = str(((g.get("winner_team_clan") or {}).get("team") or {}).get("id"))
                if a_id and w == a_id:
                    w1 += 1
                elif b_id and w == b_id:
                    w2 += 1
            s1, s2 = w1, w2
        if not s1 and not s2:
            continue  # техпоражения без карт пропускаем
        if a_id == tid:
            won = s1 > s2
        elif b_id == tid:
            won = s2 > s1
        else:
            continue
        results.append(1 if won else 0)
        if len(results) >= n:
            break

    if not results:
        return {}
    streak = 0
    for r in results:          # серия от свежей игры
        if r == results[0]:
            streak += 1
        else:
            break
    return {"wins": sum(results), "total": len(results),
            "streak": streak, "streak_won": bool(results[0]),
            "last": results}


async def team_stats(team: dict, months: int = 6, limit: int = 50):
    """Карты + форма + числа игр ОДНИМ запросом истории (экономит лимиты bo3.gg).

    V109: возврат (maps, form, games) — games для шринкейджа малых выборок.
    """
    hist = await team_history(team, months=months, limit=limit)
    maps, games = maps_from_history(hist, team["id"], return_games=True)
    if not maps:
        raise RuntimeError(f"нет карт в истории bo3 для '{team['name']}'")
    return maps, form_from_history(hist, team["id"]), games


async def get_team_stats(name: str):
    """Публичный метод: (винрейты карт, форма, числа игр) по названию.

    V114 самолечение: кэш может указывать на мёртвую команду (пустая
    история) — тогда делаем СВЕЖИЙ поиск с проверкой кандидатов по
    истории и перечитываем данные. Кэш после этого содержит правильный id.
    """
    team = await _team_by_id_fast(name)
    if team:
        try:
            return await team_stats(team)
        except RuntimeError as e:
            if "нет карт в истории" not in str(e):
                raise
            log.info("bo3: кэш '%s' -> %s (%s) пуст — переискать",
                     name, team.get("id"), team.get("name"))
    team = await find_team(name, fresh=True, validate=True)
    if not team:
        raise RuntimeError(f"команда '{name}' не найдена на bo3.gg")
    return await team_stats(team)


async def get_team_maps(name: str) -> dict[str, float]:
    """Публичный метод: винрейты карт команды по её названию (V114: самолечение)."""
    team = await _team_by_id_fast(name)
    if team:
        try:
            return await history_maps(team)
        except RuntimeError as e:
            if "нет карт в истории" not in str(e):
                raise
    team = await find_team(name, fresh=True, validate=True)
    if not team:
        raise RuntimeError(f"команда '{name}' не найдена на bo3.gg")
    return await history_maps(team)


# ---------------------------------------------------------------- H2H

async def h2h_rows(t1: dict, t2: dict, months: int = 24) -> list[dict]:
    """H2H для УЖЕ НАЙДЕННЫХ команд. rows в формате БД."""
    if not t1 or not t2 or t1["id"] == t2["id"]:
        raise RuntimeError("не удалось найти пару команд на bo3.gg")
    name1, name2 = t1["name"], t2["name"]

    since = (datetime.utcnow() - timedelta(days=30 * months)).strftime("%Y-%m-%d")
    now = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    data = await _get("/api/v1/matches", {
        "page[offset]": 0, "page[limit]": 30,
        "sort": "-start_date",
        "filter[matches.status][in]": "finished",
        "filter[matches.team_ids][contains]": f"{t1['id']},{t2['id']}",
        "filter[matches.start_date][lt]": now,
        "filter[matches.start_date][gt]": since,
        "filter[matches.discipline_id][eq]": 1,
        "with": "teams,tournament,games",
    })
    rows = []
    for m in (data.get("results") or []):
        a, b = m.get("team1") or {}, m.get("team2") or {}
        s1, s2 = m.get("team1_score"), m.get("team2_score")
        if s1 is None or s2 is None:
            # счёт серии считаем по картам
            w1 = w2 = 0
            for g in (m.get("games") or []):
                w = str(((g.get("winner_team_clan") or {}).get("team") or {}).get("id"))
                if w == str(a.get("id")):
                    w1 += 1
                elif w == str(b.get("id")):
                    w2 += 1
            s1, s2 = w1, w2
        # выравниваем: t1 всегда name1
        if str(a.get("id")) == str(t1["id"]):
            rows.append({"date": (m.get("start_date") or "")[:10],
                         "t1": a.get("name") or name1, "t2": b.get("name") or name2,
                         "score": f"{s1}:{s2}", "event": _event_name(m)})
        else:
            rows.append({"date": (m.get("start_date") or "")[:10],
                         "t1": b.get("name") or name2, "t2": a.get("name") or name1,
                         "score": f"{s2}:{s1}", "event": _event_name(m)})
    return rows


async def get_h2h(name1: str, name2: str, months: int = 24) -> list[dict]:
    """Публичный метод: H2H по названиям команд (V114: свежий переиск,
    если одна из сторон не разрешилась)."""
    t1 = await _team_by_id_fast(name1)
    t2 = await _team_by_id_fast(name2)
    if not t1:
        t1 = await find_team(name1, fresh=True, validate=True)
    if not t2:
        t2 = await find_team(name2, fresh=True, validate=True)
    return await h2h_rows(t1, t2, months=months)


def _event_name(m: dict) -> str:
    t = m.get("tournament") or {}
    return t.get("name") or ""


# ---------------------------------------------------------------- матчи дня

async def upcoming_matches(date_iso: str) -> tuple[list[dict], dict]:
    """
    Матчи на дату. Возврат: (матчи, teams-словарь bo3).
    Матч: {bo3_id, slug, start, bo, tier, id1, id2, n1, n2, logo1, logo2, odds}
    """
    data = await _get("/api/v2/matches/upcoming", {
        "date": date_iso, "utc_offset": 0,
        "filter[discipline_id][eq]": 1,
    })
    teams = (data.get("included") or {}).get("teams") or {}
    tiered = ((data.get("data") or {}).get("tiers") or {})
    out = []
    for tier_name, tier in tiered.items():
        for m in (tier.get("matches") or []):
            t1, t2 = teams.get(str(m.get("team1_id"))), teams.get(str(m.get("team2_id")))
            if not t1 or not t2:
                continue
            bet = m.get("bet_updates") or {}
            odds = None
            b1, b2 = (bet.get("team_1") or {}).get("coeff"), (bet.get("team_2") or {}).get("coeff")
            if b1 and b2:
                odds = (float(b1), float(b2))
            out.append({
                "bo3_id": m.get("id"), "slug": m.get("slug") or "",
                "start": (m.get("start_date") or "")[:16],
                "bo": m.get("bo_type"), "tier": m.get("tier") or tier_name,
                "id1": str(m.get("team1_id")), "id2": str(m.get("team2_id")),
                "n1": t1.get("name") or "", "n2": t2.get("name") or "",
                "logo1": t1.get("image_url") or "", "logo2": t2.get("image_url") or "",
                "odds": odds,
            })
    return out, teams


async def live_matches(date_iso: str) -> tuple[list[dict], dict]:
    """Идущие сейчас матчи (для лайв-раздела)."""
    data = await _get("/api/v2/matches/live", {
        "date": date_iso, "utc_offset": 0,
        "filter[discipline_id][eq]": 1,
    })
    teams = (data.get("included") or {}).get("teams") or {}
    tiered = ((data.get("data") or {}).get("tiers") or {})
    out = []
    for tier_name, tier in tiered.items():
        for m in (tier.get("matches") or []):
            t1, t2 = teams.get(str(m.get("team1_id"))), teams.get(str(m.get("team2_id")))
            if not t1 or not t2:
                continue
            out.append({
                "bo3_id": m.get("id"), "slug": m.get("slug") or "",
                "start": (m.get("start_date") or "")[:16],
                "bo": m.get("bo_type"), "tier": m.get("tier") or tier_name,
                "id1": str(m.get("team1_id")), "id2": str(m.get("team2_id")),
                "n1": t1.get("name") or "", "n2": t2.get("name") or "",
                "logo1": t1.get("image_url") or "", "logo2": t2.get("image_url") or "",
                "status": m.get("status"),
                "s1": m.get("team1_score"), "s2": m.get("team2_score"),
            })
    return out, teams
