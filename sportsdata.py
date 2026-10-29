"""
sportsdata.py — ЭТАП 3.5 (V105): слой внешних источников данных.

Роли источников:
  • PandaScore  — ПЕРВИЧНЫЙ: HTTP JSON без браузера.
                  /csgo/matches/upcoming  — ближайшие матчи (логотипы, турниры)
                  /csgo/matches/running   — идущие сейчас (лайв, счёт серии)
  • API-Sport   — РЕЗЕРВНЫЙ: HTTP JSON, два ключа с АВТОРОТАЦИЕЙ.
                  /v2/esports/matches/live, /v2/esports/matches
                  Лимит тестового плана: 100 запросов/сутки на ключ, поэтому
                  по умолчанию вызывается ТОЛЬКО когда PandaScore недоступен.
                  Кросс-проверка при каждом сборе — флагом APISPORT_CROSSCHECK=1.
  • HLTV        — по-прежнему источник винрейтов карт и H2H (Camoufox, collector).

Парная логика: get_upcoming()/get_live() сначала спрашивают PandaScore;
пусто или ошибка — бесшовно берут API-Sport (с ротацией ключей).
Ротация: round-robin; 401/403 — ключ спит 1 час; 429 — спит 60 сек;
локальный счётчик дневной квоты, чтобы не жечь лимит впустую.

Мэтчинг команд: norm_name + алиасы (odds_api) + нечёткое сравнение
(RapidFuzz, при отсутствии — difflib). Подтверждённые пары сохраняются
в БД (entity_map) — повторный мэтчинг мгновенный и без двусмысленностей.

Единый формат матча (unified) — один и тот же независимо от источника:
  {uid, title, status, begin_at, begin_msk, tournament,
   teams: [{name, id, logo, sources}], series_score, current_game,
   games_total, streams, sources, odds}
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import aiohttp

import config
import database as db
from odds_api import norm_name, names_match, ALIASES

log = logging.getLogger("sportsdata")

PANDASCORE_BASE = "https://api.pandascore.co"
APISPORT_BASE = "https://api.api-sport.ru"

DEBUG_DIR = config.BASE_DIR / "debug_odds"
DEBUG_DIR.mkdir(exist_ok=True)

STATUS_MAP = {
    "running": "live",
    "not_started": "not_started",
    "finished": "finished",
}


# ================================================================ HTTP

async def _get_json(url: str, *, headers: dict | None = None,
                    params: dict | None = None, timeout: int = 25):
    """
    GET -> (status, data|None). Никогда не бросает исключений наружу
    (кроме asyncio.CancelledError) — источники не должны ронять бота.
    """
    try:
        to = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(
                timeout=to,
                headers={"User-Agent": "cs2-analytics-bot/1.0"}) as session:
            async with session.get(url, headers=headers,
                                   params=params) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        return 200, json.loads(text)
                    except ValueError:
                        log.warning("%s: 200, но тело не JSON (%d байт)",
                                    url, len(text))
                        return 200, None
                # ошибка: короткое тело вернём для диагностики
                snippet = text[:300] if isinstance(text, str) else None
                log.debug("%s -> HTTP %s: %s", url, resp.status, snippet)
                return resp.status, None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("%s: %s", url, e)
        return 0, None


# ================================================================ ротация ключей API-Sport

class KeyRotator:
    """
    Round-robin по ключам с паузами и локальным счётчиком дневной квоты.
    401/403 -> ключ спит 1 час (битый/отозванный), 429 -> 60 секунд.
    """

    def __init__(self, keys: list[str], daily_limit: int):
        self.keys = [k for k in keys if k]
        self.daily_limit = max(1, int(daily_limit))
        self._idx = 0
        self._cooldown: dict[str, float] = {k: 0.0 for k in self.keys}
        self._day = self._today()
        self._used_today = 0

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _roll_day(self):
        today = self._today()
        if today != self._day:
            self._day = today
            self._used_today = 0
            log.info("Ротатор API-Sport: новый день, квота обнулена")

    def can_spend(self, n: int = 1) -> bool:
        self._roll_day()
        return self._used_today + n <= self.daily_limit

    def spend(self, n: int = 1):
        self._roll_day()
        self._used_today += n

    @property
    def used_today(self) -> int:
        self._roll_day()
        return self._used_today

    def available_keys(self) -> list[str]:
        now = time.time()
        return [k for k in self.keys if self._cooldown.get(k, 0) <= now]

    def next_key(self) -> str | None:
        keys = self.available_keys()
        if not keys:
            return None
        key = keys[self._idx % len(keys)]
        self._idx += 1
        return key

    def report(self, key: str, status: int):
        if status in (401, 403):
            self._cooldown[key] = time.time() + 3600
            log.warning("API-Sport ключ ...%s отклонён (HTTP %s) — пауза 1 час. "
                        "Проверь ключ в личном кабинете app.api-sport.ru",
                        key[-6:], status)
        elif status == 429:
            self._cooldown[key] = time.time() + 60
            log.warning("API-Sport ключ ...%s упёрся в лимит (429) — пауза 60 сек",
                        key[-6:])


# ================================================================ источники

class PandaScoreSource:
    source = "pandascore"

    def __init__(self, token: str):
        self.token = (token or "").strip()
        self.ok = bool(self.token)

    async def _get(self, path: str, params: dict | None = None):
        if not self.ok:
            return 0, None
        p = {"token": self.token}
        p.update(params or {})
        st, data = await _get_json(f"{PANDASCORE_BASE}{path}", params=p)
        if st in (401, 403):
            log.warning("PandaScore: ключ отклонён (HTTP %s) — проверь токен", st)
        elif st == 429:
            log.warning("PandaScore: лимит запросов (429) — подожду немного")
        return st, data

    async def upcoming(self, per_page: int = 60) -> list:
        st, data = await self._get("/csgo/matches/upcoming",
                                   {"per_page": per_page, "sort": "begin_at"})
        return data if (st == 200 and isinstance(data, list)) else []

    async def running(self, per_page: int = 30) -> list:
        st, data = await self._get("/csgo/matches/running",
                                   {"per_page": per_page})
        return data if (st == 200 and isinstance(data, list)) else []


async def pandascore_team_players(ps_id: str | int) -> list[str]:
    """V109: текущий состав команды с PandaScore (список никнеймов).

    GET /csgo/teams/{id} — у команды есть players[] (name — ник).
    Пустой список = «не получили» (нет ключа / нет команды / ошибка сети) —
    монитор ростеров просто пропускает команду, ничего не падает.
    """
    ps = PandaScoreSource(config.PANDASCORE_TOKEN)
    if not ps.ok:
        return []
    st, data = await ps._get(f"/csgo/teams/{ps_id}")
    if st != 200 or not isinstance(data, dict):
        return []
    out = []
    for p in (data.get("players") or []):
        if isinstance(p, dict) and p.get("name"):
            out.append(str(p["name"]).strip())
        elif isinstance(p, str) and p.strip():
            out.append(p.strip())
    return out


def _as_list(data) -> list:
    """API-Sport отдаёт то список, то словарь с обёрткой — снимаем аккуратно."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "results", "matches", "events"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


class ApiSportSource:
    source = "api_sport"
    _sample_dumped = False

    def __init__(self, rotator: KeyRotator):
        self.rot = rotator
        self.ok = bool(rotator.keys)

    async def _get(self, path: str, params: dict | None = None,
                   key: str | None = None):
        """
        С ротацией (key=None) или прямым ключом (для диагностики usage).
        Возврат: (status, data, used_key).
        """
        if key is not None:
            st, data = await _get_json(f"{APISPORT_BASE}{path}",
                                       headers={"Authorization": key},
                                       params=params or {})
            return st, data, key
        if not self.ok:
            return 0, None, None
        if not self.rot.can_spend():
            log.info("API-Sport: локальный лимит дневной квоты исчерпан "
                     "(%s/%s) — пропускаю",
                     self.rot.used_today, self.rot.daily_limit)
            return 0, None, None
        for _ in range(len(self.rot.keys)):
            k = self.rot.next_key()
            if not k:
                break
            st, data = await _get_json(f"{APISPORT_BASE}{path}",
                                       headers={"Authorization": k},
                                       params=params or {})
            self.rot.spend()
            if st in (401, 403, 429):
                self.rot.report(k, st)
                continue                     # пробуем следующий ключ
            return st, data, k
        return 0, None, None

    async def live(self) -> list:
        st, data, _ = await self._get("/v2/esports/matches/live")
        return _as_list(data) if st == 200 else []

    async def matches_by_date(self, date_iso: str) -> list:
        st, data, _ = await self._get(
            "/v2/esports/matches",
            {"date": date_iso, "limit": 60, "page_size": 60,
             "exclude_amateur": "true"})
        if st == 200:
            items = _as_list(data)
            self._maybe_dump_sample(items)
            return items
        return []

    async def usage(self, key: str) -> dict | None:
        """Квота конкретного ключа (для диагностики; тратит 1 запрос)."""
        st, data, _ = await self._get("/v2/account/usage", key=key)
        return data if (st == 200 and isinstance(data, dict)) else None

    def _maybe_dump_sample(self, items: list):
        """Первый непустой ответ сохраняем на диск — по нему докрутим парсер."""
        if not items or ApiSportSource._sample_dumped:
            return
        try:
            path = DEBUG_DIR / f"apisport_sample_{datetime.now():%Y%m%d}.json"
            path.write_text(json.dumps(items[:5], ensure_ascii=False,
                                       indent=1), encoding="utf-8")
            ApiSportSource._sample_dumped = True
            log.info("API-Sport: образец ответа сохранён в %s", path.name)
        except Exception as e:
            log.debug("dump sample: %s", e)


# ================================================================ единый формат

def _fmt_msk(iso: str) -> str:
    """ISO-дата -> '29.10 14:30' по таймзоне из config."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return ""
        return dt.astimezone(ZoneInfo(config.TZ_NAME)).strftime("%d.%m %H:%M")
    except Exception:
        return ""


def _pick_streams(streams_list) -> list[dict]:
    out = []
    for s in streams_list or []:
        url = s.get("raw_url") or s.get("embed_url") or ""
        if url:
            out.append({"lang": s.get("language") or "?", "url": url,
                        "main": bool(s.get("main"))})
    out.sort(key=lambda s: (s["lang"] != "ru", not s["main"]))
    return out


def normalize_ps_match(m: dict) -> dict | None:
    """PandaScore-матч -> единый формат (или None, если разобрать нельзя)."""
    try:
        opps = m.get("opponents") or []
        if len(opps) < 2:
            return None
        t1, t2 = opps[0].get("opponent") or {}, opps[1].get("opponent") or {}
        n1, n2 = t1.get("name"), t2.get("name")
        if not n1 or not n2:
            return None
        res = {r.get("team_id"): r.get("score")
               for r in (m.get("results") or [])}
        s1, s2 = res.get(t1.get("id")), res.get(t2.get("id"))
        games = m.get("games") or []
        finished = sum(1 for g in games if g.get("status") == "finished")
        league = (m.get("league") or {}).get("name") or ""
        tour = (m.get("tournament") or {}).get("name") or ""
        begin = m.get("begin_at") or m.get("scheduled_at") or ""
        return {
            "uid": f"ps:{m.get('id')}",
            "title": f"{n1} vs {n2}",
            "status": STATUS_MAP.get(m.get("status"), m.get("status") or "unknown"),
            "begin_at": begin,
            "begin_msk": _fmt_msk(begin),
            "tournament": " · ".join(x for x in (league, tour) if x),
            "teams": [
                {"name": n1, "id": t1.get("id"), "logo": t1.get("image_url") or "",
                 "sources": {"pandascore": t1.get("id")}},
                {"name": n2, "id": t2.get("id"), "logo": t2.get("image_url") or "",
                 "sources": {"pandascore": t2.get("id")}},
            ],
            "series_score": [s1, s2] if (s1 is not None and s2 is not None) else None,
            "current_game": min(finished + 1, m.get("number_of_games") or 99),
            "games_total": m.get("number_of_games") or (len(games) or None),
            "streams": _pick_streams(m.get("streams_list")),
            "sources": ["pandascore"],
            "odds": {},
        }
    except Exception as e:
        log.debug("normalize_ps_match: %s", e)
        return None


def normalize_as_match(m: dict) -> dict | None:
    """
    API-Sport-матч -> единый формат. Точная схема ответа станет известна
    по образцу в debug_odds/apisport_sample_*.json — поэтому парсер
    перебирает несколько распространённых раскладок и не падает.
    """
    try:
        teams = m.get("teams") or []
        t1 = t2 = None
        if len(teams) >= 2:
            t1, t2 = teams[0], teams[1]
        else:
            t1 = m.get("home") or m.get("team1") or m.get("homeTeam")
            t2 = m.get("away") or m.get("team2") or m.get("awayTeam")
        if not t1 or not t2:
            return None

        def _tname(t):
            return (t.get("name") or t.get("title") or
                    t.get("shortName") or "") if isinstance(t, dict) else ""

        n1, n2 = _tname(t1), _tname(t2)
        if not n1 or not n2:
            return None

        raw_status = str(m.get("status") or "").lower()
        if isinstance(m.get("status"), dict):
            raw_status = str((m.get("status") or {}).get("code") or "").lower()
        st = "live" if "live" in raw_status else (
            "finished" if ("end" in raw_status or "finish" in raw_status or
                           "closed" in raw_status) else "not_started")
        scores = m.get("score") or m.get("scores") or {}
        s1 = s2 = None
        if isinstance(scores, dict):
            s1 = scores.get("home") if scores.get("home") is not None else scores.get("homeScore")
            s2 = scores.get("away") if scores.get("away") is not None else scores.get("awayScore")
        begin = (m.get("begin_at") or m.get("start_at") or
                 m.get("date") or m.get("kickoff_at") or "")
        begin = begin.strip()
        tour = ""
        t = m.get("tournament") or m.get("league") or m.get("competition")
        if isinstance(t, dict):
            tour = t.get("name") or t.get("title") or ""
        elif isinstance(t, str):
            tour = t
        return {
            "uid": f"as:{m.get('id')}",
            "title": f"{n1} vs {n2}",
            "status": st,
            "begin_at": begin,
            "begin_msk": _fmt_msk(begin),
            "tournament": tour,
            "teams": [
                {"name": n1, "id": t1.get("id") if isinstance(t1, dict) else None,
                 "logo": (t1.get("logo") or t1.get("image") or "") if isinstance(t1, dict) else "",
                 "sources": {"api_sport": t1.get("id") if isinstance(t1, dict) else None}},
                {"name": n2, "id": t2.get("id") if isinstance(t2, dict) else None,
                 "logo": (t2.get("logo") or t2.get("image") or "") if isinstance(t2, dict) else "",
                 "sources": {"api_sport": t2.get("id") if isinstance(t2, dict) else None}},
            ],
            "series_score": [s1, s2] if (s1 is not None and s2 is not None) else None,
            "current_game": None,
            "games_total": None,
            "streams": [],
            "sources": ["api_sport"],
            "odds": {},
        }
    except Exception as e:
        log.debug("normalize_as_match: %s", e)
        return None


# ================================================================ мэтчинг команд

def fuzzy_ratio(a: str, b: str) -> float:
    """0..100. RapidFuzz, если стоит; иначе difflib (медленнее, но работает)."""
    try:
        from rapidfuzz import fuzz
        return float(fuzz.token_set_ratio(a, b))
    except Exception:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def _alias_hit(a: str, b: str) -> bool:
    """Оба имени — из одной группы алиасов (NAVI = Natus Vincere и т.д.)?"""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    for key, variants in ALIASES.items():
        group = {norm_name(key)} | {norm_name(v) for v in variants}
        if na in group and nb in group:
            return True
    return False


_ACADEMY_WORDS = ("academy", "junior", "youth", "fe", "women", "talent",
                  "school", "next", "nxt", "young", "pro")


def _is_academyish(name: str) -> bool:
    words = set(norm_name(name or "").split())
    return any(w in words for w in _ACADEMY_WORDS)


def best_team_match(name: str, candidates: list[tuple]) -> tuple | None:
    """
    Ищем команду среди кандидатов (hltv_id, hltv_name, logo).
    Правила (чтобы 'Spirit' не склеивался с 'Spirit Academy'):
      • точное совпадение после нормализации — 100;
      • алиасы (NAVI <-> Natus Vincere) — 95, без ограничения на число слов;
      • подстрока/последнее слово — 94, только при равном числе слов;
      • нечёткое >= FUZZY_MIN — при равном числе слов;
      • V114: академии/молодёжки в кандидатах штрафуются, если в запросе
        их нет (PandaScore 'Spirit' не должен ложиться на 'Spirit Academy');
      • если второй кандидат отстаёт меньше чем на 2 очка — считаем
        двусмысленностью и отказываемся (безопаснее не угадать).
    Возврат: (hltv_id, hltv_name, logo, score) | None.
    """
    nq = norm_name(name)
    if not nq or not candidates:
        return None
    q_acad = _is_academyish(name)
    scored = []
    for cid, cname, clogo in candidates:
        nc = norm_name(cname or "")
        if not nc:
            continue
        penalty = 0.0
        if not q_acad and _is_academyish(cname):
            penalty = 15.0      # V114: жёсткий штраф вместо склейки
        if nc == nq:
            if penalty:
                # точное имя-кандидат с академией ('Academy' в запросе нет) —
                # пропускаем целиком: совпадение не точное, а суффиксное
                continue
            scored.append((100.0, cid, cname, clogo))
            continue
        same_words = len(nc.split()) == len(nq.split())
        if _alias_hit(name, cname):
            scored.append((95.0 - penalty, cid, cname, clogo))
            continue
        if names_match(name, cname):
            if same_words:
                scored.append((94.0 - penalty, cid, cname, clogo))
            continue
        if same_words:
            fr = fuzzy_ratio(nq, nc)
            if fr >= config.FUZZY_MIN:
                scored.append((fr - penalty, cid, cname, clogo))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) >= 2 and scored[0][0] - scored[1][0] < 2.0:
        return None
    return (scored[0][1], scored[0][2], scored[0][3], scored[0][0])


def save_pair_ps_hltv(ps_team: dict, hltv: tuple):
    """Подтверждённая пара PandasScore <-> HLTV в БД (мэтчинг сущностей)."""
    try:
        db.save_entity_pair(
            "pandascore", ps_team.get("id"), ps_team.get("name") or "",
            "hltv", hltv[0], hltv[1] or "", float(hltv[3] if len(hltv) > 3 else 100.0))
    except Exception as e:
        log.debug("save_pair: %s", e)


def lookup_pair_ps_hltv(ps_id) -> str | None:
    """Ранее сохранённый HLTV-ID для PandaScore-команды (мгновенный мэтчинг)."""
    try:
        for row in db.get_entity_pairs("pandascore", "hltv"):
            if str(row["a_id"]) == str(ps_id):
                return str(row["b_id"])
    except Exception:
        pass
    return None


# ================================================================ кэш и парные запросы

_ps: PandaScoreSource | None = None
_as: ApiSportSource | None = None
_rot: KeyRotator | None = None

_cache = {
    "upcoming": (0.0, []),   # (timestamp, unified list)
    "live": (0.0, []),
}


def _sources():
    """Ленивая инициализация источников из config (токены из .env)."""
    global _ps, _as, _rot
    if _ps is None:
        _ps = PandaScoreSource(config.PANDASCORE_TOKEN)
        if not _ps.ok:
            log.warning("PandaScore: PANDASCORE_TOKEN не задан в .env — "
                        "источник отключён")
    if _as is None:
        _rot = KeyRotator(config.APISPORT_TOKENS, config.APISPORT_DAILY_LIMIT)
        _as = ApiSportSource(_rot)
        if not _as.ok:
            log.warning("API-Sport: APISPORT_TOKENS не заданы в .env — "
                        "источник отключён")
    return _ps, _as


def _cache_get(key: str, ttl: int) -> list | None:
    ts, data = _cache[key]
    if data and time.time() - ts < ttl:
        return data
    return None


def _cache_put(key: str, data: list):
    _cache[key] = (time.time(), data)


async def get_upcoming(force: bool = False) -> list[dict]:
    """
    Ближайшие матчи в едином формате. Пара источников:
    PandaScore -> (пусто/ошибка) -> API-Sport. Кэш MATCHES_SRC_TTL.
    """
    if not force:
        cached = _cache_get("upcoming", config.MATCHES_SRC_TTL)
        if cached is not None:
            return cached

    ps, asrc = _sources()
    unified: list[dict] = []

    try:
        raw = await ps.upcoming()
        unified = [u for u in (normalize_ps_match(m) for m in raw) if u]
        if raw and not unified:
            log.warning("PandaScore: ответ есть (%d), но ни один матч не "
                        "разобрался", len(raw))
    except Exception as e:
        log.warning("PandaScore upcoming: %s", e)

    crosscheck_needed = config.APISPORT_CROSSCHECK or not unified
    if crosscheck_needed and asrc.ok:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            raw = await asrc.matches_by_date(today)
            extra = [u for u in (normalize_as_match(m) for m in raw) if u]
            if unified and extra:
                unified = cross_fill(unified, extra)
            elif extra:
                unified = extra
        except Exception as e:
            log.warning("API-Sport matches: %s", e)

    _cache_put("upcoming", unified)
    log.info("Источники: список матчей — %d шт. (pandascore=%s)",
             len(unified), ps.ok)
    return unified


async def get_live(force: bool = False) -> list[dict]:
    """
    Идущие сейчас матчи. Пара источников: PandaScore running ->
    API-Sport live. Кэш LIVE_TTL (лайв должен быть свежим).
    """
    if not force:
        cached = _cache_get("live", config.LIVE_TTL)
        if cached is not None:
            return cached

    ps, asrc = _sources()
    unified: list[dict] = []

    try:
        raw = await ps.running()
        unified = [u for u in (normalize_ps_match(m) for m in raw) if u]
    except Exception as e:
        log.warning("PandaScore running: %s", e)

    if not unified and asrc.ok:
        try:
            raw = await asrc.live()
            unified = [u for u in (normalize_as_match(m) for m in raw) if u]
        except Exception as e:
            log.warning("API-Sport live: %s", e)

    _cache_put("live", unified)
    log.info("Источники: лайв — %d шт.", len(unified))
    return unified


def cross_fill(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """
    Склейка двух списков матчей (мэтчинг по названию пары).
    Если пара найдена в обоих источниках — объединяем: sources, логотипы,
    трансляции, счёт. Служит для режима APISPORT_CROSSCHECK=1.
    """
    def key_of(u: dict) -> str:
        a, b = sorted(norm_name(t["name"]) for t in u["teams"])
        return f"{a}|{b}"

    sec_by_key = {key_of(u): u for u in secondary if all(t.get("name") for t in u["teams"])}

    merged = []
    for u in primary:
        k = key_of(u)
        other = sec_by_key.get(k)
        if other is None:
            # мягкая попытка: нечёткое сравнение заголовков
            for k2, cand in sec_by_key.items():
                if fuzzy_ratio(k, k2) >= config.FUZZY_MIN:
                    other = cand
                    break
        if other:
            u = dict(u)
            u["sources"] = sorted(set(u["sources"]) | set(other["sources"]))
            for i in (0, 1):
                mine, theirs = u["teams"][i], other["teams"][i]
                if not mine.get("logo") and theirs.get("logo"):
                    mine["logo"] = theirs["logo"]
                if theirs.get("sources"):
                    mine["sources"].update(theirs["sources"])
                # пары ID сохраняем в entity_map
                if mine.get("sources").get("pandascore") and theirs.get("sources").get("api_sport"):
                    try:
                        db.save_entity_pair(
                            "pandascore", mine["sources"]["pandascore"],
                            mine.get("name") or "",
                            "api_sport", theirs["sources"]["api_sport"],
                            theirs.get("name") or "", 100.0)
                    except Exception:
                        pass
            if not u.get("series_score") and other.get("series_score"):
                u["series_score"] = other["series_score"]
            if not u.get("streams") and other.get("streams"):
                u["streams"] = other["streams"]
        merged.append(u)
    return merged


async def resolve_matches_to_hltv(unified: list[dict]) -> tuple[list[dict], int]:
    """
    Сопоставляет команды из единого формата с HLTV-ID из базы.
    Возврат: (строки для таблицы matches, сколько матчей не сопоставлено).
    Строка: {team1_id, team1_name, team1_logo, team2_..., start_time, tournament}.
    Матчи, где хоть одна команда не нашлась, пропускаются (не гадаем).

    V114:
      • кандидаты — ТОЛЬКО настоящие HLTV-команды (числовой id): раньше в
        кандидатах висели и bo3-команды с id 'b...', и 'ps:...' — два
        одинаковых имени => «двусмысленность» => почти всё не сопоставлялось;
      • ПАРНЫЙ мэтчинг по справочнику HLTV-матчей дня: если обе команды
        матча бьются с одной строкой HLTV (точное/алиас/нечёткое) —
        ссылаем обе с сохранением в entity_map, даже если поодиночке
        имена не дотягивали порог.
    """
    teams_rows = db.get_all_teams()
    candidates = [(str(t["hltv_id"]), t["name"] or "", t["logo_url"] or "")
                  for t in teams_rows if str(t["hltv_id"]).isdigit()]
    if not candidates:
        return [], len(unified)

    hltv_pairs = [(r["match_id"], r["team1_name"], r["team2_name"])
                  for r in (db.get_hltv_matches() or [])
                  if r.get("team1_name") and r.get("team2_name")]

    resolved_cache: dict[str, tuple | None] = {}
    out: list[dict] = []
    unresolved = 0
    seen_pairs = set()

    def _name_hit(a: str, b: str) -> float | None:
        """Схлестываются ли два имени одной команды (0..100 или None)."""
        na, nb = norm_name(a), norm_name(b)
        if not na or not nb:
            return None
        if na == nb:
            return 100.0
        if _alias_hit(a, b):
            return 95.0
        if len(na.split()) == len(nb.split()):
            fr = fuzzy_ratio(na, nb)
            if fr >= max(config.FUZZY_MIN, 88):
                return fr
        return None

    for u in unified:
        t1, t2 = u["teams"][0], u["teams"][1]
        n1, n2 = t1.get("name") or "", t2.get("name") or ""

        r1 = r2 = None
        # --- 0) парный мэтчинг по справочнику HLTV-матчей (самый надёжный).
        # Принимаем только ОДНОЗНАЧНОЕ совпадение: ровно одна пара команд
        # HLTV бьётся с парой матча — иначе не гадаем.
        hits = []
        for mid, h1, h2 in hltv_pairs:
            s11 = _name_hit(n1, h1)
            s22 = _name_hit(n2, h2)
            if s11 and s22:
                hits.append((mid, (h1, s11), (h2, s22)))
                continue
            s12 = _name_hit(n1, h2)
            s21 = _name_hit(n2, h1)
            if s12 and s21:
                hits.append((mid, (h2, s12), (h1, s21)))
        distinct = {tuple(sorted((norm_name(x[1][0]), norm_name(x[2][0]))))
                    for x in hits}
        pair_hit = hits[0] if len(distinct) == 1 else None
        if pair_hit:
            row1 = next((c for c in candidates if norm_name(c[1]) == norm_name(pair_hit[1][0])), None)
            row2 = next((c for c in candidates if norm_name(c[1]) == norm_name(pair_hit[2][0])), None)
            if row1 and row2:
                r1 = (row1[0], row1[1], row1[2], pair_hit[1][1])
                r2 = (row2[0], row2[1], row2[2], pair_hit[2][1])
                save_pair_ps_hltv(t1, r1)
                save_pair_ps_hltv(t2, r2)

        def _resolve(team: dict):
            ps_id = str(team.get("id") or "")
            name = team.get("name") or ""
            # 1) уже сохранённый мэтчинг — мгновенно
            hit = lookup_pair_ps_hltv(ps_id) if ps_id else None
            if hit:
                row = next((c for c in candidates if c[0] == hit), None)
                if row:
                    return (row[0], row[1], row[2], 100.0)
            # 2) нечёткий/точный поиск по именам
            if name not in resolved_cache:
                resolved_cache[name] = best_team_match(name, candidates)
            found = resolved_cache[name]
            if found:
                save_pair_ps_hltv(team, found)
            return found

        if not r1:
            r1 = _resolve(t1)
        if not r2:
            r2 = _resolve(t2)
        if not r1 or not r2 or r1[0] == r2[0]:
            unresolved += 1
            continue
        pair = tuple(sorted((r1[0], r2[0])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        # логотип из PandaScore — только если в базе пусто (HLTV-логотипы не трогаем)
        def _logo(hltv_hit, ps_team):
            if hltv_hit[2]:
                return None
            return ps_team.get("logo") or None

        out.append({
            "team1_id": r1[0], "team1_name": r1[1], "team1_logo": _logo(r1, t1),
            "team2_id": r2[0], "team2_name": r2[1], "team2_logo": _logo(r2, t2),
            "start_time": u.get("begin_msk") or "",
            "tournament": u.get("tournament") or "",
        })
    return out, unresolved


def find_hltv_match_smart(name1: str, name2: str) -> str | None:
    """
    V114: HLTV-id матча по паре имён — быстрый точный поиск (db), затем
    алиасы/нечёткость ('NAVI' <-> 'Natus Vincere' и т.п.). Для Лайв-центра
    и дайджестов: раньше немапнутое имя источника = матча «нет на HLTV».
    """
    try:
        hit = db.find_hltv_match(name1, name2)
        if hit:
            return hit
        a1, a2 = norm_name(name1), norm_name(name2)
        if not a1 or not a2:
            return None
        for r in db.get_hltv_matches(max_age=10 * 3600):
            h1, h2 = r.get("team1_name") or "", r.get("team2_name") or ""
            if not h1 or not h2:
                continue
            for x, y in (((h1, h2), (h2, h1))):
                if _name_match_team(name1, x) and _name_match_team(name2, y):
                    return str(r["match_id"])
        return None
    except Exception as e:
        log.debug("find_hltv_match_smart: %s", e)
        return None


def _name_match_team(a: str, b: str) -> bool:
    """Одна команда по именам? (точное / алиас / нечёткое при равных словах)"""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb or _alias_hit(a, b):
        return True
    if len(na.split()) == len(nb.split()):
        return fuzzy_ratio(na, nb) >= max(config.FUZZY_MIN, 88)
    return False


async def diagnostics() -> dict:
    """Сводка для test_datasources.py: живость источников и мэтчинг."""
    ps, asrc = _sources()
    report: dict = {"pandascore": {}, "api_sport": {}, "matcher": {}}

    if ps.ok:
        t0 = time.time()
        up = await ps.upcoming(per_page=20)
        report["pandascore"]["upcoming_latency"] = round(time.time() - t0, 2)
        report["pandascore"]["upcoming"] = len(up)
        t0 = time.time()
        run = await ps.running()
        report["pandascore"]["running_latency"] = round(time.time() - t0, 2)
        report["pandascore"]["running"] = len(run)
        sample = next(iter(up), None)
        if sample:
            u = normalize_ps_match(sample)
            report["pandascore"]["sample_ok"] = bool(u)

    for i, key in enumerate(asrc.rot.keys, 1):
        usage = await asrc.usage(key)
        report["api_sport"][f"key{i}_tail"] = key[-6:]
        if usage:
            plan = usage.get("plan") or {}
            u = usage.get("usage") or {}
            report["api_sport"][f"key{i}_plan"] = plan.get("name")
            report["api_sport"][f"key{i}_remaining"] = u.get("remaining")
            report["api_sport"][f"key{i}_services"] = usage.get("services")
        else:
            report["api_sport"][f"key{i}_error"] = "недоступен/неверный"

    teams_rows = db.get_all_teams()
    candidates = [(str(t["hltv_id"]), t["name"] or "", t["logo_url"] or "")
                  for t in teams_rows]
    report["matcher"]["db_teams"] = len(candidates)
    if ps.ok and candidates:
        raw = await ps.upcoming(per_page=30)
        pairs = []
        checked = 0
        for m in raw[:30]:
            u = normalize_ps_match(m)
            if not u:
                continue
            checked += 2
            for t in u["teams"]:
                hit = best_team_match(t["name"], candidates)
                if hit:
                    pairs.append((t["name"], hit[1], round(hit[3])))
        report["matcher"]["checked"] = checked
        report["matcher"]["matched_pairs"] = pairs[:15]
        report["matcher"]["matched_count"] = len(pairs)
    return report
