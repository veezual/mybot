"""
roster_watch.py — V109: «живая аналитика» смен ростеров.

Идея: статистика карт/формы в базе набрана СТАРЫМ составом. Если вчера
в команде заменили игрока, модель об этом не знает и уверенно несёт чушь.

Как работает:
  1) kick_roster_watch() — фоновая задача (не блокирует бота), раз в
     ROSTER_TTL (4 часа) обходит команды матчей дня;
  2) для каждой команды берём её PandaScore-ID (из id матча или entity_map)
     и тянем текущий состав: GET /csgo/teams/{id} -> players[];
  3) сравниваем с сохранённым составом (team_rosters). Первая загрузка —
     просто сохраняем состав (это база для будущих сравнений, не алерт);
     последующие: состав изменился -> пишем алерт в roster_alerts
     (кто ушёл, кто пришёл, когда);
  4) predict_match получает свежие алерты: причина в карточке +
     потолок уверенности ROSTER_CONF_CAP (статистика старым составом);
  5) value_model блокирует ставки по командам со свежей сменой ростера.

Всё безопасно: PandaScore отвалится — монитор молча пропустит команду,
бот работает как раньше.
"""
import asyncio
import logging
import time

import config
import database as db
import sportsdata

log = logging.getLogger("roster_watch")

_roster_lock = asyncio.Lock()
_roster_last_run = 0.0
_ps_id_cache: dict[str, str | None] = {}   # team_id -> ps_id (или None)


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def diff_players(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """Разница составов (без регистра/пробелов): (пришли, ушли)."""
    old_set = {_norm(p) for p in (old or []) if p}
    new_set = {_norm(p) for p in (new or []) if p}
    added = [p for p in (new or []) if p and _norm(p) not in old_set]
    removed = [p for p in (old or []) if p and _norm(p) not in new_set]
    return added, removed


def ps_id_for(team_id: str) -> str | None:
    """PandaScore-ID команды: сам id (ps:...) или пара из entity_map.

    В entity_map матчер сохраняет пары pandascore<->hltv (и pandascore<->b:).
    Результат кэшируется в памяти — за день команда не меняет ps-id.
    """
    tid = str(team_id)
    if tid in _ps_id_cache:
        return _ps_id_cache[tid]
    ps_id: str | None = None
    if tid.startswith("ps:"):
        ps_id = tid[3:]
    else:
        try:
            for pair in db.get_entity_pairs("pandascore", "hltv"):
                if str(pair["b_id"]) == tid:
                    ps_id = str(pair["a_id"])
                    break
            if not ps_id:
                for pair in db.get_entity_pairs("pandascore", "api_sport"):
                    if str(pair["b_id"]) == tid:
                        ps_id = str(pair["a_id"])
                        break
        except Exception as e:
            log.debug("ps_id_for(%s): %s", tid, e)
    _ps_id_cache[tid] = ps_id
    return ps_id


_ensure_fail: dict[str, float] = {}   # team_id -> время последней неудачи (V112)


async def ensure_roster(team_id: str, team_name: str) -> list[str]:
    """V112: состав команды для карточки прогноза. Кэш БД -> PandaScore.

    Пустой список = состав неизвестен (нет ps-id / PandaScore не ответил) —
    вызывающий просто не показывает блок составов. Неудачные команды не
    дёргаем повторно 30 минут.
    """
    tid = str(team_id)
    r = db.get_roster(tid)
    if r and r.get("players"):
        return list(r["players"])
    if time.time() - _ensure_fail.get(tid, 0.0) < 30 * 60:
        return []
    ps_id = ps_id_for(tid)
    if not ps_id:
        _ensure_fail[tid] = time.time()
        return []
    try:
        players = await sportsdata.pandascore_team_players(ps_id)
    except Exception as e:
        log.info("состав %s: запрос не удался (%s)", team_name, e)
        players = []
    if players and len(players) >= 3:
        db.save_roster(tid, players)
        _ensure_fail.pop(tid, None)
        return list(players)
    _ensure_fail[tid] = time.time()
    return []


async def _check_team(team_id: str, team_name: str) -> dict | None:
    """Проверка одной команды: вернёт алерт (dict) или None.

    Возврат None — нет изменений/нет данных. Алерт сохраняется в БД.
    """
    ps_id = ps_id_for(team_id)
    if not ps_id:
        return None
    try:
        players = await sportsdata.pandascore_team_players(ps_id)
    except Exception as e:
        log.info("ростер %s: запрос не удался (%s)", team_name, e)
        return None
    if not players or len(players) < 3:
        # 0-2 «игрока» — PandaScore отдал мусор/неполные данные, не алертим
        return None

    old = db.get_roster(team_id)
    db.save_roster(team_id, players)
    if not old or not old.get("players"):
        return None   # первая загрузка — базы для сравнения ещё не было

    added, removed = diff_players(old["players"], players)
    if not added and not removed:
        return None

    db.save_roster_alert(team_id, team_name, added, removed)
    alert = {"team_name": team_name, "added": added, "removed": removed,
             "created_at": time.time()}
    log.info("СМЕНА РОСТЕРА %s: +%s / -%s", team_name, added, removed)
    return alert


async def run_roster_watch() -> list[dict]:
    """Один проход по командам матчей дня. Возврат: найденные алерты."""
    alerts: list[dict] = []
    matches = db.get_matches()
    teams: dict[str, str] = {}
    for m in matches:
        for tid, nm in ((m["team1_id"], m["team1_name"]),
                        (m["team2_id"], m["team2_name"])):
            if tid and nm and str(tid) not in teams:
                teams[str(tid)] = nm
    for tid, nm in teams.items():
        # не дёргаем команду, если состав проверялся недавно
        r = db.get_roster(tid)
        if r and (time.time() - (r.get("updated_at") or 0)) < config.ROSTER_TTL:
            continue
        try:
            a = await _check_team(tid, nm)
        except Exception as e:
            log.info("ростер %s: %s", nm, e)
            a = None
        if a:
            alerts.append(a)
        await asyncio.sleep(0.5)   # бережём лимиты PandaScore
    return alerts


def kick_roster_watch():
    """Фоновый запуск прохода (не блокирует, дубли не плодит)."""
    async def _task():
        global _roster_last_run   # V110: без этого UnboundLocalError на каждом запуске
        if _roster_lock.locked():
            return
        async with _roster_lock:
            try:
                if time.time() - _roster_last_run < 300:
                    return   # чаще раза в 5 минут не проверяем
                _roster_last_run = time.time()
                alerts = await run_roster_watch()
                if alerts:
                    log.info("Ростер-монитор: %d смен(ы) обнаружено", len(alerts))
            except Exception as e:
                log.warning("ростер-монитор: %s", e)
    return asyncio.create_task(_task())
