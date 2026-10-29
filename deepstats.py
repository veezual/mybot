"""
deepstats.py — V113: глубокие данные HLTV для прогнозов и AI-разбора.

Что даёт:
  • рейтинги игроков — HLTV /stats/players?team={id} (rating 2.1, 3 месяца,
    с числом карт) — главный «человеческий» фактор модели;
  • мировой ранг команд — HLTV /ranking/teams/ (одна страница = весь топ-30).

Принципы (те же, что во всём боте):
  • ВСЁ ходит через общий Camoufox-синглтон collector.get_browser() — второй
    браузер не запускается никогда;
  • ВСЁ кэшируется в SQLite: прогноз и value-скан работают только по кэшу
    (мгновенно, без сети), свежие данные добираются фоном или по явному
    запросу AI-кнопки;
  • любая ошибка — тихий пропуск: прогноз остаётся валидным без глубоких
    данных (компоненты просто выпадают, веса перенормируются).
"""
import asyncio
import logging
import time

import config
import database as db

log = logging.getLogger("deepstats")

_ps_fail: dict[str, float] = {}      # team_id -> время последней неудачи
_rank_fail_ts = 0.0                  # последняя неудача сбора топа
_deep_lock = asyncio.Lock()          # свои задачи — последовательно
_warmup_last_run = 0.0


def _is_hltv_id(team_id) -> bool:
    """Глубокие данные есть только у команд с настоящим HLTV-ID."""
    return str(team_id or "").strip().isdigit()


# --------------------------------------------------------- рейтинги игроков

async def _fetch_players_via_browser(team_id: str, team_name: str) -> list[dict]:
    import collector
    from hltv_api import fetch_player_stats
    browser = await collector.get_browser()
    page = await collector.new_page_clean(browser)
    try:
        return await fetch_player_stats(page, team_id, team_name,
                                        months=config.PLAYER_STATS_MONTHS)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def ensure_player_stats(team_id, team_name: str,
                              force: bool = False) -> list[dict]:
    """Рейтинги игроков команды: кэш -> HLTV (Camoufox).

    Пустой список = данных нет (нет HLTV-ID / Cloudflare не пустил / кулдаун
    после неудачи). Прогноз и карточка спокойно живут без них.
    """
    tid = str(team_id or "").strip()
    if not _is_hltv_id(tid):
        return []
    rows = db.get_player_stats(tid)
    age = db.player_stats_age(tid)
    if not force and rows and age is not None and age < config.PLAYER_STATS_TTL:
        return rows
    if not force and time.time() - _ps_fail.get(tid, 0.0) < config.DEEP_FAIL_COOLDOWN:
        return db.get_player_stats(tid)

    async with _deep_lock:
        # перепроверка после очереди (двойной клик / параллельные задачи)
        rows = db.get_player_stats(tid)
        age = db.player_stats_age(tid)
        if not force and rows and age is not None and age < config.PLAYER_STATS_TTL:
            return rows
        try:
            fresh = await _fetch_players_via_browser(tid, team_name)
        except Exception as e:
            log.info("рейтинги игроков %s: %s", team_name, e)
            _ps_fail[tid] = time.time()
            return db.get_player_stats(tid)
        if fresh:
            db.save_player_stats(tid, fresh)
            _ps_fail.pop(tid, None)
            return fresh
        _ps_fail[tid] = time.time()
        return rows


def cached_player_stats(team_id) -> list[dict]:
    """Рейтинги ИЗ КЭША (без сети) — для прогноза и value-скана."""
    tid = str(team_id or "").strip()
    if not _is_hltv_id(tid):
        return []
    return db.get_player_stats(tid)


def players_power_input(rows: list[dict]) -> list[dict]:
    """Отбор игроков для модели: с рейтингом и достаточной выборкой."""
    out = []
    for r in rows or []:
        try:
            maps = int(r.get("maps") or 0)
        except (TypeError, ValueError):
            maps = 0
        rating = r.get("rating")
        if rating is None or maps < config.PLAYER_MIN_MAPS:
            continue
        out.append({"nickname": r.get("nickname") or "?",
                    "rating": float(rating), "maps": maps})
    return out


# --------------------------------------------------------- мировой ранг

async def _fetch_rankings_via_browser() -> dict:
    import collector
    from hltv_api import fetch_team_rankings
    browser = await collector.get_browser()
    page = await collector.new_page_clean(browser)
    try:
        return await fetch_team_rankings(page)
    finally:
        try:
            await page.close()
        except Exception:
            pass


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def rank_for(team_name: str, rankings: dict | None = None) -> int | None:
    """Ранг команды из КЭША топа (без сети). None — команды нет в топ-30."""
    if not team_name:
        return None
    if rankings is None:
        rankings, _age = db.get_rankings()
    if not rankings:
        return None
    key = _norm(team_name)
    if key in {_norm(k) for k in rankings}:
        for k, v in rankings.items():
            if _norm(k) == key:
                return v.get("rank")
        return None
    # мягкий фолбэк: "Natus Vincere" ~ "NAVI" и т.п. — по вхождению
    best, best_len = None, 0
    for k, v in rankings.items():
        kn = _norm(k)
        if not kn or not key:
            continue
        if kn in key or key in kn:
            if len(kn) > best_len:
                best, best_len = v.get("rank"), len(kn)
    return best


async def ensure_rankings(force: bool = False) -> dict:
    """Топ HLTV: кэш -> браузер. Возврат словарь (возможно пустой)."""
    global _rank_fail_ts
    data, age = db.get_rankings()
    if data and not force and age is not None and age < config.RANK_TTL:
        return data
    if not force and time.time() - _rank_fail_ts < config.DEEP_FAIL_COOLDOWN:
        return data or {}
    async with _deep_lock:
        data, age = db.get_rankings()
        if data and not force and age is not None and age < config.RANK_TTL:
            return data
        try:
            fresh = await _fetch_rankings_via_browser()
        except Exception as e:
            log.info("мировой ранг: %s", e)
            _rank_fail_ts = time.time()
            return data or {}
        if fresh:
            db.save_rankings(fresh)
            _rank_fail_ts = 0.0
            return fresh
        _rank_fail_ts = time.time()
        return data or {}


# --------------------------------------------------------- пара для AI

async def ensure_deep_pair(id1, name1: str, id2, name2: str,
                           status_cb=None) -> tuple[list[dict], list[dict],
                                                    int | None, int | None]:
    """Полный глубокий пакет по паре (с сетью). Для AI-разбора.

    status_cb(text) — тексты прогресса. Ошибки глотаются: вернётся то,
    что успели/что было в кэше.
    """
    p1 = await ensure_player_stats(id1, name1)
    if status_cb:
        await status_cb(f"⏳ Рейтинги {name1}: "
                        + (f"{len(p1)} игроков" if p1 else "нет данных, еду дальше"))
    p2 = await ensure_player_stats(id2, name2)
    if status_cb:
        await status_cb(f"⏳ Рейтинги {name2}: "
                        + (f"{len(p2)} игроков" if p2 else "нет данных"))
    ranks = await ensure_rankings()
    return (p1, p2, rank_for(name1, ranks), rank_for(name2, ranks))


# --------------------------------------------------------- фоновый прогрев

def kick_deep_warmup():
    """Фоновый прогрев глубоких данных по командам матчей дня.

    Не блокирует: рейтинги игроков и топ HLTV лягут в кэш к моменту,
    когда пользователь откроет прогноз или AI-разбор.
    """
    async def _task():
        global _warmup_last_run
        if _deep_lock.locked():
            return
        async with _deep_lock:
            if time.time() - _warmup_last_run < 300:
                return
            _warmup_last_run = time.time()
            try:
                await ensure_rankings()
            except Exception as e:
                log.info("прогрев топа: %s", e)
            teams: dict[str, str] = {}
            for m in db.get_matches():
                for tid, nm in ((m["team1_id"], m["team1_name"]),
                                (m["team2_id"], m["team2_name"])):
                    if _is_hltv_id(tid) and tid not in teams and nm:
                        teams[tid] = nm
            done = 0
            for tid, nm in list(teams.items())[:config.DEEP_WARMUP_MAX]:
                age = db.player_stats_age(tid)
                if age is not None and age < config.PLAYER_STATS_TTL:
                    continue
                if time.time() - _ps_fail.get(tid, 0.0) < config.DEEP_FAIL_COOLDOWN:
                    continue
                try:
                    rows = await _fetch_players_via_browser(tid, nm)
                    if rows:
                        db.save_player_stats(tid, rows)
                        _ps_fail.pop(tid, None)
                        log.info("прогрев игроков %s: %s шт.", nm, len(rows))
                    else:
                        _ps_fail[tid] = time.time()
                except Exception as e:
                    _ps_fail[tid] = time.time()
                    log.info("прогрев игроков %s: %s", nm, e)
                done += 1
                if done >= config.DEEP_WARMUP_MAX:
                    break
                await asyncio.sleep(1.0)   # бережём HLTV/Cloudflare
            log.info("Глубокий прогрев завершён (%d команд)", done)

    return asyncio.create_task(_task())
