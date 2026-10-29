"""
database.py — SQLite-хранилище.

ГЛАВНОЕ ИСПРАВЛЕНИЕ против V99:
  Везде ключом команды служит HLTV-ID (строка, напр. "7020" у Spirit).
  В старой версии save_team(t_id, t_name) путал аргументы, из-за чего
  в поле name попадали числа, а team_maps был несогласован с teams.

Схема готова к Этапу 2 (коэффициенты БК): таблица odds.
"""
import sqlite3
import time
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("database")

from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    # timeout=15 (busy_timeout) — V106: параллельные задачи больше не валятся
    # с 'database is locked', а спокойно ждут своей очереди до 15 секунд.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # Команды. Ключ — HLTV-ID.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            hltv_id   TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            logo_url  TEXT,
            updated_at REAL
        )
    """)

    # Винрейты по картам (в процентах, float).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_maps (
            hltv_id    TEXT NOT NULL,
            map_name   TEXT NOT NULL,
            winrate    REAL NOT NULL,
            updated_at REAL,
            PRIMARY KEY (hltv_id, map_name)
        )
    """)

    # Матчи дня (полностью перезаписывается при каждом сборе).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team1_id    TEXT, team1_name TEXT,
            team2_id    TEXT, team2_name TEXT,
            start_time  TEXT,
            updated_at  REAL
        )
    """)

    # Личные встречи. Пара хранится отсортированной: min(id1,id2) / max(...).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS h2h (
            team1_id   TEXT NOT NULL,
            team2_id   TEXT NOT NULL,
            match_date TEXT,
            t1_name    TEXT,
            t2_name    TEXT,
            score      TEXT,
            event      TEXT,
            updated_at REAL,
            PRIMARY KEY (team1_id, team2_id, match_date, score, event)
        )
    """)

    #KeyValue-метаданные (время последнего сбора и т.п.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ЭТАП 2: коэффициенты букмекерских контор.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS odds (
            match_key TEXT NOT NULL,      -- "id1:id2" пары
            bookmaker TEXT NOT NULL,      -- fonbet / pari / winline / betboom
            market    TEXT NOT NULL,      -- "w1" / "w2" / "map1_w1" ...
            value     REAL NOT NULL,      -- коэффициент
            updated_at REAL,
            PRIMARY KEY (match_key, bookmaker, market)
        )
    """)

    # ЭТАП 3: value-сигналы (история рекомендаций модели).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS value_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key   TEXT,
            team1       TEXT, team2 TEXT,
            pick        TEXT,
            bookmaker   TEXT,
            odd         REAL,
            model_p     REAL,
            market_p    REAL,
            edge        REAL,
            ev          REAL,
            kelly       REAL,
            status      TEXT DEFAULT 'pending',   -- pending / win / lose
            created_at  REAL,
            resolved_at REAL
        )
    """)

    # ЭТАП 3.5 (V105): соответствия сущностей между источниками
    # (PandasScore <-> HLTV <-> API-Sport). Заполняется мэтчером в sportsdata.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entity_map (
            source_a   TEXT NOT NULL,     -- "pandascore" / "api_sport" / "hltv"
            a_id       TEXT NOT NULL,
            source_b   TEXT NOT NULL,
            b_id       TEXT NOT NULL,
            a_name     TEXT,
            b_name     TEXT,
            score      REAL,
            updated_at REAL,
            PRIMARY KEY (source_a, a_id, source_b, b_id)
        )
    """)

    # V106: форма команд (последние матчи) для прогнозной модели.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_form (
            hltv_id     TEXT PRIMARY KEY,
            wins        INTEGER,
            total       INTEGER,
            streak      INTEGER,
            streak_won  INTEGER,
            last10      TEXT,
            updated_at  REAL
        )
    """)

    # ЭТАП 3.5 (V105): турнир у матча (для будущей кнопки «Турниры»).
    try:
        cur.execute("ALTER TABLE matches ADD COLUMN tournament TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

    # V109: число сыгранных карт у каждого винрейта (для шринкейджа).
    try:
        cur.execute("ALTER TABLE team_maps ADD COLUMN games INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

    # V109: текущие составы (монитор ростеров, PandaScore).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_rosters (
            hltv_id     TEXT PRIMARY KEY,
            players     TEXT,
            updated_at  REAL
        )
    """)

    # V109: алерты о сменах ростера («живая аналитика»).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS roster_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hltv_id     TEXT NOT NULL,
            team_name   TEXT,
            added       TEXT,
            removed     TEXT,
            source      TEXT,
            created_at  REAL
        )
    """)

    # V111: список матчей HLTV с id страницы матча (для Лайв-центра /
    # scorebot). Живёт отдельно от matches — тот перезаписывается
    # источниками (PandaScore/bo3), а HLTV-id терять нельзя.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hltv_matches (
            match_id    TEXT PRIMARY KEY,
            team1_name  TEXT,
            team2_name  TEXT,
            t1_id       TEXT,
            t2_id       TEXT,
            start_time  TEXT,
            updated_at  REAL
        )
    """)

    # V113: рейтинги игроков HLTV (rating 2.1 за 3 месяца).
    # Один ряд на игрока команды; старые записи команды вычищаются при записи.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_stats (
            hltv_id     TEXT NOT NULL,
            nickname    TEXT NOT NULL,
            rating      REAL,
            maps        INTEGER,
            kd          REAL,
            updated_at  REAL,
            PRIMARY KEY (hltv_id, nickname)
        )
    """)

    # V114: отмеченные матчи (звёздочка) — фоновые дайджесты счёта в чат.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            uid         TEXT PRIMARY KEY,
            name1       TEXT,
            name2       TEXT,
            chat_id     INTEGER,
            added_at    REAL,
            last_ts     REAL DEFAULT 0,
            last_text   TEXT DEFAULT '',
            misses      INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    log.info("База данных инициализирована: %s", DB_PATH)


# ---------------- teams ----------------

def upsert_team(hltv_id: str, name: str, logo_url: str | None = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO teams (hltv_id, name, logo_url, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(hltv_id) DO UPDATE SET
            name=excluded.name,
            logo_url=COALESCE(excluded.logo_url, teams.logo_url),
            updated_at=excluded.updated_at
    """, (str(hltv_id), name, logo_url, time.time()))
    conn.commit()
    conn.close()


def get_team(hltv_id: str):
    conn = get_connection()
    row = conn.execute("SELECT * FROM teams WHERE hltv_id = ?", (str(hltv_id),)).fetchone()
    conn.close()
    return row


def get_all_teams():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
    conn.close()
    return rows


# ---------------- карты ----------------

def replace_maps(hltv_id: str, maps, games: dict | None = None) -> None:
    """Полностью обновляет набор карт команды.

    V106: принимает И dict {карта: процент}, И список пар [(карта, %), ...].
    Раньше dict приводил к 'too many values to unpack (expected 2)' —
    из-за этого карты bo3/HLTV молча не сохранялись в базу.
    V109: games — опциональный {карта: число карт в выборке} для шринкейджа.
    V115: запись идёт во ВСЕ известные id команды (HLTV id, bo3 'b…', ps:…).
    Раньше стата писалась под тем id, каким назвал команду последний
    источник матчей (HLTV-браузер / PandaScore+bo3-добор), и при смене
    источника покрытие «пропадало» — прогрев собирал всё заново.
    """
    if isinstance(maps, dict):
        items = []
        for k, v in maps.items():
            try:
                items.append((str(k).strip(), float(v)))
            except (TypeError, ValueError):
                continue
    else:
        items = []
        for item in (maps or []):
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    items.append((str(item[0]).strip(), float(item[1])))
            except (TypeError, ValueError):
                continue
    games = games or {}
    ids = _alias_ids(hltv_id)   # V115: сам id + все алиасы имени
    conn = get_connection()
    now = time.time()
    for aid in ids:
        conn.execute("DELETE FROM team_maps WHERE hltv_id = ?", (aid,))
        conn.executemany(
            """INSERT INTO team_maps (hltv_id, map_name, winrate, games, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(aid, m, wr, int(games.get(m, 0)), now) for m, wr in items],
        )
    conn.commit()
    conn.close()


def get_maps(hltv_id: str) -> tuple[dict[str, float], float]:
    """Возврат: ({карта: винрейт}, время последнего обновления)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT map_name, winrate, updated_at FROM team_maps WHERE hltv_id = ?",
        (str(hltv_id),),
    ).fetchall()
    conn.close()
    if not rows:
        return {}, 0.0
    newest = max(r["updated_at"] or 0 for r in rows)
    return {r["map_name"]: r["winrate"] for r in rows}, newest


def maps_age(hltv_id: str) -> float | None:
    """Возврат: возраст данных в секундах либо None, если данных нет."""
    _, ts = get_maps(hltv_id)
    return (time.time() - ts) if ts else None


def maps_age_any(hltv_id: str) -> float | None:
    """V115: возраст карт по любому из алиасов id (для проверок свежести
    прогрева — раньше проверка была слепа к соседнему namespace)."""
    _, ts = get_maps_any(hltv_id)
    return (time.time() - ts) if ts else None


# ---------------- форма команд (V106) ----------------

def replace_form(hltv_id: str, form: dict) -> None:
    """Сохраняет форму: {wins, total, streak, streak_won, last} [1/0, ...].
    V115: запись во все алиасы id (аналогично replace_maps)."""
    if not form or not form.get("total"):
        return
    conn = get_connection()
    now = time.time()
    for aid in _alias_ids(hltv_id):
        conn.execute("""
            INSERT INTO team_form (hltv_id, wins, total, streak, streak_won, last10, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hltv_id) DO UPDATE SET
                wins=excluded.wins, total=excluded.total, streak=excluded.streak,
                streak_won=excluded.streak_won, last10=excluded.last10,
                updated_at=excluded.updated_at
        """, (aid, int(form["wins"]), int(form["total"]),
              int(form.get("streak", 0)), 1 if form.get("streak_won") else 0,
              json.dumps(form.get("last") or []), now))
    conn.commit()
    conn.close()


def get_form(hltv_id: str) -> dict | None:
    """Форма команды либо None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM team_form WHERE hltv_id = ?", (str(hltv_id),)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        last = json.loads(row["last10"] or "[]")
    except Exception:
        last = []
    if not row["total"]:
        return None
    return {"wins": row["wins"], "total": row["total"],
            "streak": row["streak"], "streak_won": bool(row["streak_won"]),
            "last": last, "updated_at": row["updated_at"]}


def form_age(hltv_id: str) -> float | None:
    f = get_form(hltv_id)
    if not f:
        return None
    return time.time() - (f.get("updated_at") or 0)


def form_age_any(hltv_id: str) -> float | None:
    """V115: возраст формы по любому из алиасов id."""
    f = get_form_any(hltv_id)
    if not f:
        return None
    return time.time() - (f.get("updated_at") or 0)


# ------------- V107: алиасы id между источниками -------------
# Одна и та же команда живёт в базе под разными ключами: HLTV "13286",
# PandaScore "ps:20832", api-sport "b20832". Из-за этого данные (форма,
# карты), сохранённые под одним ключом, не находились под другим.
# Алиасы берём из entity_map (мэтчер sportsdata) и из teams (по имени).

def _alias_ids(hltv_id: str) -> list[str]:
    """Все известные id одной команды в других неймспейсах (включая сам id)."""
    hid = str(hltv_id)
    out, seen = [hid], {hid}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT a_id, b_id FROM entity_map WHERE a_id = ? OR b_id = ?",
            (hid, hid),
        ).fetchall()
        for r in rows:
            for v in (r["a_id"], r["b_id"]):
                v = str(v or "").strip()
                if v and v not in seen:
                    seen.add(v)
                    out.append(v)
        # одна команда могла сохраниться в teams под разными id с одним именем
        name_row = conn.execute(
            "SELECT name FROM teams WHERE hltv_id = ?", (hid,)).fetchone()
        if name_row and name_row["name"]:
            same = conn.execute(
                "SELECT hltv_id FROM teams WHERE lower(trim(name)) = lower(trim(?))",
                (name_row["name"],),
            ).fetchall()
            for r in same:
                v = str(r["hltv_id"])
                if v not in seen:
                    seen.add(v)
                    out.append(v)
    except sqlite3.Error as e:
        log.debug("alias_ids(%s): %s", hid, e)
    finally:
        conn.close()
    return out


def get_form_any(hltv_id: str) -> dict | None:
    """V107: форма по любому известному id команды (сам id + алиасы)."""
    for hid in _alias_ids(hltv_id):
        f = get_form(hid)
        if f:
            return f
    return None


def get_maps_any(hltv_id: str) -> tuple[dict[str, float], float]:
    """V107: карты по любому известному id команды (самые свежие из найденных)."""
    best: tuple[dict[str, float], float] = ({}, 0.0)
    for hid in _alias_ids(hltv_id):
        m, ts = get_maps(hid)
        if m and ts >= best[1]:
            best = (m, ts)
    return best


def get_map_games(hltv_id: str) -> dict[str, int]:
    """V109: число карт в выборке по каждой карте (для шринкейджа)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT map_name, games FROM team_maps WHERE hltv_id = ?",
            (str(hltv_id),),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out: dict[str, int] = {}
    for r in rows:
        try:
            out[str(r["map_name"])] = int(r["games"] or 0)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return out


def get_map_games_any(hltv_id: str) -> dict[str, int]:
    """V109: число игр по картам через алиасы id (как get_maps_any)."""
    for hid in _alias_ids(hltv_id):
        g = get_map_games(hid)
        if g:
            return g
    return {}


# ---------------- V113: глубокие данные HLTV ----------------

def save_player_stats(hltv_id: str, rows: list[dict]) -> None:
    """Рейтинги игроков HLTV: [{nickname, rating, maps, kd}, ...].

    Старые записи команды вычищаются: состав мог измениться, а рейтинг
    ушедшего игрока не должен висеть за новым.
    """
    conn = get_connection()
    tid = str(hltv_id)
    now = time.time()
    try:
        conn.execute("DELETE FROM player_stats WHERE hltv_id = ?", (tid,))
        for r in rows:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO player_stats
                       (hltv_id, nickname, rating, maps, kd, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tid, str(r.get("nickname") or "")[:48],
                     float(r["rating"]) if r.get("rating") is not None else None,
                     int(r["maps"]) if r.get("maps") is not None else None,
                     float(r["kd"]) if r.get("kd") is not None else None,
                     now))
            except (KeyError, TypeError, ValueError):
                continue
        conn.commit()
    except sqlite3.Error as e:
        log.warning("save_player_stats(%s): %s", tid, e)
    finally:
        conn.close()


def get_player_stats(hltv_id: str) -> list[dict]:
    """Рейтинги игроков команды (свежесть проверяет caller по player_stats_age)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT nickname, rating, maps, kd, updated_at FROM player_stats "
            "WHERE hltv_id = ? ORDER BY rating DESC", (str(hltv_id),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [{"nickname": r["nickname"], "rating": r["rating"],
             "maps": r["maps"], "kd": r["kd"], "updated_at": r["updated_at"]}
            for r in rows]


def player_stats_age(hltv_id: str) -> float | None:
    """Секунды с последней записи рейтингов команды (None — нет данных)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS ts FROM player_stats WHERE hltv_id = ?",
            (str(hltv_id),)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row or not row["ts"]:
        return None
    return max(0.0, time.time() - float(row["ts"]))


def save_rankings(rankings: dict) -> None:
    """Мировой ранг HLTV: {имя: {rank, points}} — целиком, JSON в meta."""
    set_meta("hltv_rankings", json.dumps(rankings, ensure_ascii=False))
    set_meta("hltv_rankings_ts", str(time.time()))


def get_rankings() -> tuple[dict | None, float | None]:
    """(словарь рангов, возраст в секундах). (None, None) — данных нет."""
    raw = get_meta("hltv_rankings", "")
    ts = get_meta("hltv_rankings_ts", "")
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except Exception:
        return None, None
    age = (time.time() - float(ts)) if ts else None
    return data, age


# ---------------- V109: ростеры и алерты о заменах ----------------

def save_roster(hltv_id: str, players: list[str], source: str = "pandascore") -> None:
    """Сохраняет текущий состав команды (список никнеймов)."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO team_rosters (hltv_id, players, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(hltv_id) DO UPDATE SET
            players=excluded.players, updated_at=excluded.updated_at
    """, (str(hltv_id), json.dumps(list(players), ensure_ascii=False), time.time()))
    conn.commit()
    conn.close()


def get_roster(hltv_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM team_rosters WHERE hltv_id = ?", (str(hltv_id),)).fetchone()
    except sqlite3.Error:
        row = None   # таблицы ещё нет (init_db не запускался) — не падаем
    finally:
        conn.close()
    if not row:
        return None
    try:
        players = json.loads(row["players"] or "[]")
    except Exception:
        players = []
    return {"players": players, "updated_at": row["updated_at"]}


def save_roster_alert(hltv_id: str, team_name: str, added: list[str],
                      removed: list[str], source: str = "pandascore") -> None:
    """Фиксирует смену ростера (вызывается, когда состав изменился)."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO roster_alerts (hltv_id, team_name, added, removed, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(hltv_id), team_name,
         json.dumps(list(added), ensure_ascii=False),
         json.dumps(list(removed), ensure_ascii=False),
         source, time.time()))
    conn.commit()
    conn.close()


def get_roster_alert(hltv_id: str, fresh_days: int = 7) -> dict | None:
    """Свежий алерт о смене ростера команды (или None)."""
    cut = time.time() - fresh_days * 86400
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM roster_alerts WHERE hltv_id = ? AND created_at >= ?
               ORDER BY id DESC LIMIT 1""", (str(hltv_id), cut)).fetchone()
    except sqlite3.Error:
        row = None   # таблицы ещё нет — не падаем
    finally:
        conn.close()
    if not row:
        return None
    try:
        added = json.loads(row["added"] or "[]")
        removed = json.loads(row["removed"] or "[]")
    except Exception:
        added, removed = [], []
    return {"team_name": row["team_name"], "added": added, "removed": removed,
            "source": row["source"], "created_at": row["created_at"]}


def get_roster_alert_any(hltv_id: str, fresh_days: int = 7) -> dict | None:
    """V109: свежий алерт по любому известному id команды (алиасы)."""
    for hid in _alias_ids(hltv_id):
        a = get_roster_alert(hid, fresh_days)
        if a:
            return a
    return None


# ---------------- матчи дня ----------------

def replace_matches(matches: list[dict]):
    conn = get_connection()
    now = time.time()
    conn.execute("DELETE FROM matches")
    conn.executemany(
        """INSERT INTO matches
           (team1_id, team1_name, team2_id, team2_name, start_time, tournament, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (m["team1_id"], m["team1_name"], m["team2_id"], m["team2_name"],
             m.get("start_time", ""), m.get("tournament", ""), now)
            for m in matches
        ],
    )
    conn.commit()
    conn.close()


def get_matches() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM matches ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- V111: HLTV-матчи (id страниц для scorebot) ----------------

def replace_hltv_matches(matches: list[dict]):
    """Обновить справочник матчей HLTV (с id страницы). Старые строки
    старше суток вычищаем, чтобы не копить мусор."""
    conn = get_connection()
    now = time.time()
    conn.execute("DELETE FROM hltv_matches")
    conn.executemany(
        """INSERT OR REPLACE INTO hltv_matches
           (match_id, team1_name, team2_name, t1_id, t2_id, start_time, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(str(m.get("match_id") or ""), m.get("team1_name", ""),
          m.get("team2_name", ""), str(m.get("team1_id", "")),
          str(m.get("team2_id", "")), m.get("start_time", ""), now)
         for m in matches if m.get("match_id")],
    )
    conn.commit()
    conn.close()


def find_hltv_match(name1: str, name2: str, max_age: float = 6 * 3600) -> str | None:
    """Найти HLTV-id матча по паре имён команд (в любом порядке).
    Нормализация грубая, но достаточная: для Лайв-центра пара та же,
    что идёт на HLTV. Возврат: id страницы или None."""
    def _n(s: str) -> str:
        return (s or "").strip().lower().replace(" ", "")

    a, b = _n(name1), _n(name2)
    if not a or not b:
        return None
    conn = get_connection()
    rows = conn.execute(
        "SELECT match_id, team1_name, team2_name, updated_at FROM hltv_matches"
    ).fetchall()
    conn.close()
    now = time.time()
    best: str | None = None
    for r in rows:
        if now - (r["updated_at"] or 0) > max_age:
            continue
        x, y = _n(r["team1_name"]), _n(r["team2_name"])
        if (a == x and b == y) or (a == y and b == x):
            best = str(r["match_id"])
            break
    return best


def matches_age() -> float | None:
    conn = get_connection()
    row = conn.execute("SELECT MAX(updated_at) AS t FROM matches").fetchone()
    conn.close()
    ts = row["t"] if row else None
    return (time.time() - ts) if ts else None


def get_hltv_matches(max_age: float = 12 * 3600) -> list[dict]:
    """V114: все свежие HLTV-матчи справочника (для умного мэтчинга пар)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT match_id, team1_name, team2_name, t1_id, t2_id, updated_at"
        " FROM hltv_matches").fetchall()
    conn.close()
    now = time.time()
    return [dict(r) for r in rows if now - (r["updated_at"] or 0) <= max_age]


# ---------------- V114: watchlist (звёздочка / дайджесты) ----------------

def add_watch(uid: str, name1: str, name2: str, chat_id: int) -> int:
    """Отметить матч. Возврат: сколько уже отмечено у этого чата."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO watchlist (uid, name1, name2, chat_id, added_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET chat_id=excluded.chat_id""",
        (uid, name1, name2, int(chat_id), time.time()))
    n = conn.execute("SELECT COUNT(*) AS n FROM watchlist WHERE chat_id = ?",
                     (int(chat_id),)).fetchone()["n"]
    conn.commit()
    conn.close()
    return int(n)


def remove_watch(uid: str, chat_id: int | None = None):
    conn = get_connection()
    if chat_id is None:
        conn.execute("DELETE FROM watchlist WHERE uid = ?", (uid,))
    else:
        conn.execute("DELETE FROM watchlist WHERE uid = ? AND chat_id = ?",
                     (uid, int(chat_id)))
    conn.commit()
    conn.close()


def get_watchlist() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM watchlist").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def watch_update(uid: str, last_text: str = "", misses: int | None = None):
    """Обновить после отправки дайджеста (текст+время) или промаха."""
    conn = get_connection()
    if misses is None:
        conn.execute("UPDATE watchlist SET last_ts = ?, last_text = ?"
                     " WHERE uid = ?", (time.time(), last_text, uid))
    else:
        conn.execute("UPDATE watchlist SET misses = ? WHERE uid = ?",
                     (int(misses), uid))
    conn.commit()
    conn.close()


# ---------------- V114: разовая зачистка отравленного кэша bo3 ----------------

def purge_bo3_team_cache():
    """V114: одноразовая миграция. До V114 кэш bo3_team:* заполнялся
    неверными привязками (Imperial -> Imperial Pro и т.п.) и никогда
    не перепроверялся — из-за этого 'нет карт в истории bo3' по всем
    командам. Кэш чистим один раз; дальше bo3data пишет только
    проверенные историей привязки."""
    conn = get_connection()
    done = conn.execute("SELECT value FROM meta WHERE key = 'bo3_cache_purge_v114'").fetchone()
    if done:
        conn.close()
        return
    cur = conn.execute("DELETE FROM meta WHERE key LIKE 'bo3_team:%'")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                 " ('bo3_cache_purge_v114', '1')")
    conn.commit()
    conn.close()
    log.info("Миграция V114: кэш bo3_team очищен (%d записей) — привязки "
             "перестроятся с проверкой по истории", cur.rowcount)


# ---------------- V115: миграция namespace-статистики ----------------

def migrate_v115_stats_namespaces():
    """V115: одноразовая починка «двух неймспейсов».

    До V115 стата (карты/форма/H2H) писалась под тем id, каким команду
    назвал ПОСЛЕДНИЙ источник матчей: HLTV-браузер — обычные id (4773 paiN),
    PandaScore+bo3-добор — 'b'-префиксы (b637 paiN). При каждой смене
    источника покрытие «пропадало», прогрев собирал всё заново, а половина
    записей лежала мёртвым грузом.

    Миграция:
      1) копирует карты/форму/H2H с 'b'-ключей на обычные id той же команды
         (имя связывает их через teams);
      2) удаляет b-дубли, у которых есть обычный близнец;
      3) чистит мусорные отрицательные кэши (miss/net/team от старой логики
         и имён БК) — с V115 они снова наполнятся, но по честным правилам.
    """
    conn = get_connection()
    try:
        done = conn.execute(
            "SELECT value FROM meta WHERE key = 'bo3_migrate_v115'").fetchone()
        if done:
            return
        # группы "имя -> [id...]" из teams (имя связывает namespace)
        groups: dict[str, list[str]] = {}
        for r in conn.execute("SELECT hltv_id, name FROM teams"):
            nm = (r["name"] or "").strip().lower()
            if nm:
                groups.setdefault(nm, []).append(str(r["hltv_id"]))

        def plain_ids(bid: str) -> list[str]:
            row = conn.execute("SELECT name FROM teams WHERE hltv_id = ?",
                               (bid,)).fetchone()
            if not row:
                return []
            ids = [i for i in groups.get((row["name"] or "").strip().lower(), [])
                   if not i.startswith("b")]
            return ids

        copied = deleted = 0
        # --- карты ---
        for (bid,) in conn.execute(
                "SELECT DISTINCT hltv_id FROM team_maps WHERE hltv_id LIKE 'b%'").fetchall():
            targets = [t for t in plain_ids(bid) if t != bid]
            if not targets:
                continue
            rows = conn.execute(
                "SELECT map_name, winrate, games, updated_at FROM team_maps "
                "WHERE hltv_id = ?", (bid,)).fetchall()
            if not rows:
                continue
            for t in targets:
                have = conn.execute("SELECT 1 FROM team_maps WHERE hltv_id = ? LIMIT 1",
                                    (t,)).fetchone()
                if not have:
                    conn.executemany(
                        "INSERT INTO team_maps (hltv_id, map_name, winrate, games, updated_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        [(t, r["map_name"], r["winrate"], r["games"] or 0,
                          r["updated_at"]) for r in rows])
                    copied += 1
            conn.execute("DELETE FROM team_maps WHERE hltv_id = ?", (bid,))
            deleted += 1
        # --- форма ---
        for (bid,) in conn.execute(
                "SELECT hltv_id FROM team_form WHERE hltv_id LIKE 'b%'").fetchall():
            targets = [t for t in plain_ids(bid) if t != bid]
            if not targets:
                continue
            row = conn.execute("SELECT * FROM team_form WHERE hltv_id = ?", (bid,)).fetchone()
            for t in targets:
                have = conn.execute("SELECT 1 FROM team_form WHERE hltv_id = ?",
                                    (t,)).fetchone()
                if not have and row:
                    conn.execute(
                        "INSERT INTO team_form (hltv_id, wins, total, streak, streak_won,"
                        " last10, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (t, row["wins"], row["total"], row["streak"], row["streak_won"],
                         row["last10"], row["updated_at"]))
                    copied += 1
            conn.execute("DELETE FROM team_form WHERE hltv_id = ?", (bid,))
            deleted += 1
        # --- H2H пары с b-сторонами ---
        for (a, b) in conn.execute(
                "SELECT DISTINCT team1_id, team2_id FROM h2h "
                "WHERE team1_id LIKE 'b%' OR team2_id LIKE 'b%'").fetchall():
            pa, pb = plain_ids(a), plain_ids(b)
            if not pa or not pb:
                continue
            rows = conn.execute(
                "SELECT match_date, t1_name, t2_name, score, event, updated_at "
                "FROM h2h WHERE team1_id = ? AND team2_id = ?", (a, b)).fetchall()
            if not rows:
                continue
            for t1 in pa:
                for t2 in pb:
                    if t1 == t2:
                        continue
                    lo, hi = sorted([t1, t2])
                    have = conn.execute(
                        "SELECT 1 FROM h2h WHERE team1_id = ? AND team2_id = ? LIMIT 1",
                        (lo, hi)).fetchone()
                    if not have:
                        conn.executemany(
                            "INSERT OR IGNORE INTO h2h (team1_id, team2_id, match_date,"
                            " t1_name, t2_name, score, event, updated_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [(lo, hi, r["match_date"], r["t1_name"], r["t2_name"],
                              r["score"], r["event"], r["updated_at"]) for r in rows])
                        copied += 1
            conn.execute("DELETE FROM h2h WHERE team1_id = ? AND team2_id = ?", (a, b))
            deleted += 1
        # --- чистка отрицательных кэшей прошлых версий ---
        n_meta = 0
        for pref in ("bo3_miss:%", "bo3_net:%", "bo3_team:%"):
            cur = conn.execute("DELETE FROM meta WHERE key LIKE ?", (pref,))
            n_meta += cur.rowcount
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES"
                     " ('bo3_migrate_v115', '1')")
        conn.commit()
        log.info("Миграция V115: статистика переведена на общий namespace "
                 "(скопировано %d, b-дублей удалено %d, кэш-ключей вычищено %d)",
                 copied, deleted, n_meta)
    except Exception as e:
        log.warning("Миграция V115: %s (продолжаю без неё)", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


# ---------------- h2h ----------------

def replace_h2h(id1: str, id2: str, rows: list[dict]):
    """rows: [{date, t1, t2, score, event}] — пара нормализуется (min/max).
    V115: пишем во ВСЕ комбинации алиасов обеих команд (HLTV id × bo3 'b…') —
    раньше пара, собранная под одним namespace, считалась отсутствующей
    после смены источника матчей и пересобиралась заново."""
    now = time.time()
    ids1 = _alias_ids(id1)
    ids2 = _alias_ids(id2)
    conn = get_connection()
    for a in ids1:
        for b in ids2:
            if a == b:
                continue
            lo, hi = sorted([a, b])
            conn.execute("DELETE FROM h2h WHERE team1_id = ? AND team2_id = ?", (lo, hi))
            conn.executemany(
                """INSERT OR IGNORE INTO h2h
                   (team1_id, team2_id, match_date, t1_name, t2_name, score, event, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(lo, hi, r.get("date", ""), r.get("t1", ""), r.get("t2", ""),
                  r.get("score", ""), r.get("event", ""), now) for r in rows],
            )
    conn.commit()
    conn.close()


def get_h2h(id1: str, id2: str) -> list[dict]:
    a, b = sorted([str(id1), str(id2)])
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM h2h WHERE team1_id = ? AND team2_id = ?",
        (a, b),
    ).fetchall()
    conn.close()
    # ВАЖНО: отдаём в формате парсера HLTV (t1/t2, date) — именно эти ключи
    # ждут predict.h2h_summary и карточки. Раньше отдавали колонки t1_name/
    # t2_name, из-за чего H2H всегда считался 0:0 (молчаливый баг V100-V103).
    out = []
    for r in rows:
        out.append({
            "date": r["match_date"] or "",
            "t1": r["t1_name"] or "",
            "t2": r["t2_name"] or "",
            "score": r["score"] or "",
            "event": r["event"] or "",
        })
    return out


def h2h_age(id1: str, id2: str) -> float | None:
    a, b = sorted([str(id1), str(id2)])
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(updated_at) AS t FROM h2h WHERE team1_id = ? AND team2_id = ?",
        (a, b),
    ).fetchone()
    conn.close()
    ts = row["t"] if row else None
    return (time.time() - ts) if ts else None


def h2h_age_any(id1: str, id2: str) -> float | None:
    """V115: возраст H2H по любым комбинациям алиасов пары (самая свежая)."""
    best: float | None = None
    for a in _alias_ids(id1):
        for b in _alias_ids(id2):
            if a == b:
                continue
            age = h2h_age(a, b)
            if age is not None and (best is None or age < best):
                best = age
    return best


def get_h2h_any(id1: str, id2: str) -> list[dict]:
    """V107: H2H по любым известным id пары (алиасы перебираются крест-накрест).

    Пара могла быть собрана под другим неймспейсом (HLTV / ps: / b:) —
    раньше она в этом случае считалась «не собранной» и тянула повторный сбор.
    """
    for a in _alias_ids(id1):
        for b in _alias_ids(id2):
            rows = get_h2h(a, b)
            if rows:
                return rows
    return []


# ---------------- odds (коэффициенты БК, Этап 2) ----------------

def save_odds(match_key: str, bookmaker: str, w1: float, w2: float,
              sides: tuple | None = None):
    """match_key = "min_hltv_id:max_hltv_id"; sides = (норм-имя П1, норм-имя П2)."""
    conn = get_connection()
    now = time.time()
    for market, value in (("w1", float(w1)), ("w2", float(w2))):
        conn.execute("""
            INSERT INTO odds (match_key, bookmaker, market, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_key, bookmaker, market) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at
        """, (match_key, bookmaker, market, value, now))
    conn.commit()
    conn.close()
    if sides:
        set_meta(f"odds_sides:{match_key}:{bookmaker}", json.dumps(list(sides)))


def get_odds(match_key: str, max_age: float) -> dict[str, dict]:
    """Свежие кефы пары: {bookmaker: {w1, w2, sides: [имя П1, имя П2]}}."""
    cut = time.time() - max_age
    conn = get_connection()
    rows = conn.execute(
        "SELECT bookmaker, market, value FROM odds WHERE match_key = ? AND updated_at >= ?",
        (match_key, cut),
    ).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["bookmaker"], {})
        d[r["market"]] = r["value"]
    result = {}
    for bk, d in out.items():
        if "w1" not in d or "w2" not in d:
            continue
        sides = None
        raw = get_meta(f"odds_sides:{match_key}:{bk}", "")
        if raw:
            try:
                sides = json.loads(raw)
            except Exception:
                sides = None
        result[bk] = {"w1": d["w1"], "w2": d["w2"], "sides": sides or []}
    return result


def odds_age(match_key: str) -> float | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(updated_at) AS t FROM odds WHERE match_key = ?", (match_key,)
    ).fetchone()
    conn.close()
    ts = row["t"] if row else None
    return (time.time() - ts) if ts else None


def save_odds_sweep(pairs_by_key: dict[tuple, dict[str, dict]], id_lookup: dict[str, str]):
    """
    Сохраняет результаты общего сбора кефов.
    pairs_by_key: {(norm1, norm2): {bk: {team1, team2, w1, w2}}}
    id_lookup: {norm_name: hltv_id} — соответствие имён и HLTV-ID из базы.
    """
    saved = 0
    for (na, nb), per_bk in pairs_by_key.items():
        id1, id2 = id_lookup.get(na), id_lookup.get(nb)
        if not id1 or not id2:
            continue
        key = ":".join(sorted([str(id1), str(id2)]))
        for bk, p in per_bk.items():
            save_odds(key, bk, p["w1"], p["w2"], sides=(na, nb))
            saved += 1
    log.info("Кефы сохранены: %s записей", saved)
    return saved


# ---------------- value-сигналы (Этап 3) ----------------

def save_signal(match_key: str, team1: str, team2: str, pick: str,
                bookmaker: str, odd: float, model_p: float, market_p: float,
                edge: float, ev: float, kelly: float) -> int | None:
    """
    Сохраняет сигнал. Дедуп: если по этой паре+выбору+БК уже висит
    незакрытый сигнал — новый не создаём (возврат None).
    """
    conn = get_connection()
    dup = conn.execute(
        "SELECT id FROM value_signals WHERE match_key = ? AND pick = ? "
        "AND bookmaker = ? AND status = 'pending'",
        (match_key, pick, bookmaker),
    ).fetchone()
    if dup:
        conn.close()
        return None
    cur = conn.execute(
        """INSERT INTO value_signals
           (match_key, team1, team2, pick, bookmaker, odd, model_p, market_p,
            edge, ev, kelly, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (match_key, team1, team2, pick, bookmaker, odd, model_p, market_p,
         edge, ev, kelly, time.time()),
    )
    sig_id = cur.lastrowid
    conn.commit()
    conn.close()
    return sig_id


def get_signals(limit: int = 20, status: str | None = None) -> list[dict]:
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM value_signals WHERE status = ? "
            "ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM value_signals ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_signal_result(sig_id: int, status: str):
    """status: 'win' / 'lose' / 'pending' (возврат в ожидание)."""
    conn = get_connection()
    conn.execute(
        "UPDATE value_signals SET status = ?, resolved_at = ? WHERE id = ?",
        (status, time.time() if status != 'pending' else None, sig_id),
    )
    conn.commit()
    conn.close()


def signals_stats() -> dict:
    """Итоги по закрытым сигналам: винрейт и ROI при флэт-ставке 1 ед."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT odd, status FROM value_signals WHERE status IN ('win', 'lose')"
    ).fetchall()
    conn.close()
    n_win = sum(1 for r in rows if r["status"] == "win")
    n_lose = len(rows) - n_win
    profit = sum((r["odd"] - 1.0) if r["status"] == "win" else -1.0 for r in rows)
    staked = float(len(rows))
    return {
        "n_win": n_win, "n_lose": n_lose, "n_closed": len(rows),
        "hitrate": (n_win / len(rows) * 100.0) if rows else 0.0,
        "profit": profit,
        "roi": (profit / staked * 100.0) if rows else 0.0,
    }


# ---------------- entity_map (мэтчинг источников, Этап 3.5) ----------------

def save_entity_pair(source_a: str, a_id, a_name: str,
                     source_b: str, b_id, b_name: str, score: float):
    """Сохраняет подтверждённую пару «одна команда в двух источниках»."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO entity_map (source_a, a_id, source_b, b_id, a_name, b_name, score, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_a, a_id, source_b, b_id) DO UPDATE SET
            a_name=excluded.a_name, b_name=excluded.b_name,
            score=excluded.score, updated_at=excluded.updated_at
    """, (str(source_a), str(a_id), str(source_b), str(b_id),
          a_name, b_name, float(score), time.time()))
    conn.commit()
    conn.close()


def get_entity_pairs(source_a: str, source_b: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM entity_map WHERE source_a = ? AND source_b = ?",
        (str(source_a), str(source_b)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- meta ----------------

def set_meta(key: str, value: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_meta(key: str, default: str = "") -> str:
    conn = get_connection()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


if __name__ == "__main__":
    init_db()
