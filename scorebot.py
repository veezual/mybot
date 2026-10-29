"""
scorebot.py — V111: живые данные матча с HLTV scorebot (push, 1-3 сек).

Это единственный БЕСПЛАТНЫЙ источник эконом-аналитики в реальном времени
(деньги в раунде, эко/форс/фулл-бай, моментум). PandaScore отдаёт такое
только на платном Pro Live, GRID/Bayes — только enterprise.

Как устроено:
  1) В Camoufox-синглтоне открывается ОДНА скрытая страница hltv.org
     (куки Cloudflare уже в профиле — сайт открывается без челленджа);
  2) из контекста этой страницы (Firefox TLS-палец, Origin hltv.org)
     открывается WebSocket к wss://scorebot-secure.hltv.org — это тот же
     канал, которым питается лайв-страница HLTV (socket.io, EIO=3);
  3) на каждый просматриваемый матч — СВОЙ WebSocket (однозначные кадры);
  4) кадры форвардятся в Python, парсер собирает MatchState:
     счёт, раунд, стороны, деньги команд, закупы, серии, бомба, киллы;
  5) бот раз в SCOREBOT_EDIT_GAP правит лайв-карточку в Telegram.

Честные ограничения:
  • scorebot есть только у матчей, которые трекает HLTV (tier-2/3 — почти
    всегда есть);
  • протокол неофициальный: неизвестные события складываются в debug-кольцо
    (/sbdump в боте) — по дампу легко дообучить парсер;
  • стороны (T/CT) в начале матча угадываются ( team1 = CT ) и
    переворачиваются на смене сторон; если HLTV отдаст имена команд в
    стартовом пакете — привязка точная.
"""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("scorebot")

_SB_URL = "wss://scorebot-secure.hltv.org/socket.io/?EIO=3&transport=websocket"
_HLTV_ORIGIN = "https://www.hltv.org/"

# JS-мост: живёт на странице hltv.org. Один WebSocket на матч,
# автопереподключение, pong на ping, форвард кадров в Python.
# V112fix (ДВА бага в одном шве):
#  1) Playwright трактует строку-не-функцию как ВЫРАЖЕНИЕ — многострочный
#     скрипт с «;» падал: Page.evaluate: expected expression, got ')'.
#     Теперь всё завёрнуто в функцию '() => { ... }'.
#  2) Шов `"""" + _SB_URL + """"` лексер Python читал как: конец строки,
#     затем ОДИНОЧНАЯ кавычка открывает НОВУЮ строку, в которую попадает
#     ЛИТЕРАЛЬНЫЙ текст ' + _SB_URL + ' — адрес WS-сервера НИКОГДА не
#     подставлялся (и в V111 тоже)! Теперь плейсхолдер __SB_URL__.
_BRIDGE_JS = """
() => {
window.__sbWs = window.__sbWs || {};
window.__sbSub = (mid) => {
  if (window.__sbWs[mid]) return "dup";
  let want = true;
  const mk = () => {
    if (!want) return;
    let ws;
    try { ws = new WebSocket("__SB_URL__"); }
    catch (e) {
      try { window.__sbRaw(JSON.stringify({__id: mid, f: JSON.stringify({__err: String(e)})})); } catch (e2) {}
      return;
    }
    window.__sbWs[mid] = ws;
    ws.onopen = () => {
      try { window.__sbRaw(JSON.stringify({__id: mid, f: JSON.stringify({__open: 1})})); } catch (e) {}
    };
    ws.onclose = () => {
      delete window.__sbWs[mid];
      try { window.__sbRaw(JSON.stringify({__id: mid, f: JSON.stringify({__close: 1})})); } catch (e) {}
      if (want) setTimeout(mk, 5000);
    };
    ws.onmessage = (ev) => {
      const d = String(ev.data);
      if (d === "2") { try { ws.send("3"); } catch (e) {} return; }
      if (d.charAt(0) === "0") { try { ws.send("40"); } catch (e) {} return; }
      let out = null;
      if (d.indexOf("40") === 0) out = JSON.stringify({__connected: 1});
      else if (d.indexOf("42") === 0) out = d.slice(2);
      if (out) {
        try { window.__sbRaw(JSON.stringify({__id: mid, f: out})); } catch (e) {}
      }
    };
  };
  mk();
  return "ok";
};
window.__sbUnsub = (mid) => {
  const ws = window.__sbWs[mid];
  if (ws) { try { ws.onclose = null; ws.close(); } catch (e) {} delete window.__sbWs[mid]; }
  return "ok";
};
return "ok";
}
""".replace("__SB_URL__", _SB_URL)


_WIN_TYPES = {
    "elimination": "вынос", "opponents_eliminated": "вынос",
    "bombdefused": "размин", "defused": "размин",
    "bombexploded": "взрыв", "exploded": "взрыв",
    "targetbombed": "взрыв", "targetbombeddot": "взрыв",
    "time": "время", "gametime": "время", "surrender": "сдались",
}


def buy_label(avg: float | None) -> str:
    """Классификация закупа по средним деньгам на игрока."""
    if avg is None:
        return "?"
    if avg >= config.SB_FULL_BUY:
        return "ФУЛЛ-БАЙ"
    if avg >= config.SB_SEMI_BUY:
        return "ФОРС"
    if avg >= config.SB_MINI_BUY:
        return "2К-БАЙ"
    return "ЭКО"


def _g(d: dict, *keys):
    """Достать значение по вариантам имени ключа (регистр не важен)."""
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v is not None:
            return v
    return None


def _player_name(p: dict) -> str:
    return str(_g(p, "nick", "name", "nickname", "player") or "?")[:24]


def _now_msk() -> str:
    return datetime.now(ZoneInfo(config.TZ_NAME)).strftime("%H:%M:%S")


class MatchState:
    """Всё, что бот знает о живом матче, — обновляется пушем."""

    def __init__(self, mid: str, name1: str, name2: str):
        self.mid = str(mid)
        self.name1, self.name2 = name1, name2
        self.map_name = ""
        self.ct_score = 0
        self.t_score = 0
        self.side1 = "CT"            # сторона team1 (переворачивается на смене)
        self.round_no = 1
        self.money = [None, None]    # суммы $ команд (best effort)
        self.bomb = ""               # "" | "заложена" | "разминирована" | "взрыв"
        self.last_rounds: deque = deque(maxlen=8)   # {n, winner_idx, wt}
        self.kills: deque = deque(maxlen=5)         # строки
        self.streak = (None, 0)      # (индекс команды, длина серии)
        self.match_over = False
        self.got_data = False        # пришёл хоть один log-пакет
        self.broken = False          # данных нет долго — уходим на опрос
        self.players1: set[str] = set()
        self.players2: set[str] = set()
        self.updated = asyncio.Event()
        self.debug: deque = deque(maxlen=60)        # сырые неизвестные события
        self._money_players = [{}, {}]              # name -> max money за раунд
        self.subscribed_at = time.time()
        self.last_change = time.time()

    # ---------- производные для карточки ----------

    def scores(self) -> tuple[int, int]:
        """Счёт в порядке (team1, team2)."""
        s1 = self.t_score if self.side1 == "T" else self.ct_score
        s2 = self.t_score if self.side1 != "T" else self.ct_score
        return s1, s2

    def avgs(self) -> tuple[float | None, float | None]:
        out = []
        for m in self.money:
            out.append((m / 5.0) if isinstance(m, (int, float)) and m > 0 else None)
        return out[0], out[1]

    def winner_idx_of_side(self, side: str) -> int:
        return 0 if side == self.side1 else 1

    def flip_sides_if_needed(self):
        total = self.ct_score + self.t_score
        # MR12: смена сторон после 12 раундов (и каждые 12 далее для простоты)
        if total > 0 and total % 12 == 0:
            self.side1 = "T" if self.side1 == "CT" else "CT"
            # V111fix: счёт хранится ПО СТОРОНАМ (ct_score — победы стороны,
            # которая сейчас играет за CT). После смены столбцы обязаны
            # поменяться местами, иначе вторая половина идёт с зеркальным
            # счётом (было 11:1, карточка покажет 1:11 до конца матча).
            self.ct_score, self.t_score = self.t_score, self.ct_score
            log.info("scorebot %s: смена сторон (после %d раундов)", self.mid, total)

    def touch(self):
        self.last_change = time.time()
        self.updated.set()


# ================================================================ мост

class ScorebotBridge:
    """Один на бота: страница-хост + WebSockets + парсер."""

    def __init__(self):
        self._page = None
        self._js_ready = False
        self._starting = False
        self._states: dict[str, MatchState] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._router: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None

    # ---------- жизненный цикл ----------

    async def start(self):
        if self._page is not None or self._starting:
            return
        self._starting = True
        page = None
        try:
            import collector
            import hltv_api
            async with collector.lock:
                browser = await collector.get_browser()
                page = await browser.new_page()
                await page.expose_function("__sbRaw", self._on_raw)
                await page.goto(_HLTV_ORIGIN, wait_until="domcontentloaded",
                                timeout=60000)
                ok = await hltv_api.wait_cloudflare(page)
                if not ok:
                    log.warning("scorebot: hltv.org не прошёл Cloudflare — "
                                "мост может не заработать (профиль прогреется)")
                await page.evaluate(_BRIDGE_JS)
                self._page = page
                self._js_ready = True
            self._router = asyncio.create_task(self._route_loop())
            self._watchdog = asyncio.create_task(self._watchdog_loop())
            log.info("scorebot: страница-мост готова")
        except Exception as e:
            log.warning("scorebot: старт не удался (%s) — лайв будет на опросе", e)
            self._page = None
            self._js_ready = False
            if page is not None:      # V112fix: не оставлять страницу висеть
                try:
                    await page.close()
                except Exception:
                    pass
        finally:
            self._starting = False

    async def close(self):
        for t in (self._router, self._watchdog):
            if t:
                t.cancel()
        self._router = self._watchdog = None
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None
        self._js_ready = False

    # ---------- подписки ----------

    async def subscribe(self, mid: str, name1: str, name2: str) -> MatchState | None:
        mid = str(mid)
        if not config.SCOREBOT_ENABLED or not mid.isdigit():
            return None
        await self.start()
        if self._page is None:
            return None
        st = self._states.get(mid)
        if st is None:
            st = MatchState(mid, name1, name2)
            self._states[mid] = st
        try:
            await self._page.evaluate("(id) => window.__sbSub(id)", mid)
            log.info("scorebot: подписка на матч %s (%s vs %s)", mid, name1, name2)
            return st
        except Exception as e:
            log.warning("scorebot: подписка %s не удалась: %s", mid, e)
            self._states.pop(mid, None)
            return None

    async def unsubscribe(self, mid: str):
        mid = str(mid)
        self._states.pop(mid, None)
        if self._page and self._js_ready:
            try:
                await self._page.evaluate("(id) => window.__sbUnsub(id)", mid)
            except Exception:
                pass

    def state(self, mid: str) -> MatchState | None:
        return self._states.get(str(mid))

    def latest_state(self) -> MatchState | None:
        if not self._states:
            return None
        return max(self._states.values(), key=lambda s: s.last_change)

    async def wait_update(self, mid: str, timeout: float) -> bool:
        st = self._states.get(str(mid))
        if not st:
            return False
        try:
            await asyncio.wait_for(st.updated.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if st in self._states.values():
                st.updated.clear()

    # ---------- приём кадров ----------

    async def _on_raw(self, s: str):
        try:
            self._queue.put_nowait(s)
        except asyncio.QueueFull:
            pass

    async def _route_loop(self):
        while True:
            s = await self._queue.get()
            try:
                env = json.loads(s)
            except Exception:
                continue
            mid = str(env.get("__id") or "")
            st = self._states.get(mid)
            if st is None:
                continue
            frame = env.get("f") or ""
            if frame.startswith("["):
                try:
                    arr = json.loads(frame)
                except Exception:
                    continue
                name = str(arr[0]) if arr else ""
                payload = arr[1] if len(arr) > 1 else {}
                if name == "log" and isinstance(payload, dict):
                    self._handle_log(st, payload)
                else:
                    st.debug.append((time.time(), name, json.dumps(payload)[:200]))

    # ---------- парсер ----------

    def _handle_log(self, st: MatchState, payload: dict):
        st.got_data = True
        st.touch()
        self._seed(st, payload)
        events = payload.get("log")
        for k, v in payload.items():
            if str(k).lower() == "log":
                continue
            if isinstance(v, dict):
                self._apply(st, str(k), v)
        if isinstance(events, list):
            for item in events:
                if isinstance(item, dict):
                    for k, v in item.items():
                        self._apply(st, str(k), v)

    def _seed(self, st: MatchState, payload: dict):
        """Стартовый пакет: карта/команды/игроки, если HLTV их отдал."""
        if not st.map_name:
            mp = _g(payload, "mapName", "map", "mapname")
            if isinstance(mp, str) and mp:
                st.map_name = mp
        for key, bucket in (("t1", st.players1), ("t2", st.players2),
                            ("team1", st.players1), ("team2", st.players2)):
            raw = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                raw = _g(raw, "players", "team")
            if isinstance(raw, list):
                for p in raw:
                    if isinstance(p, dict):
                        nm = _player_name(p)
                        if nm and nm != "?":
                            bucket.add(nm)
        # явная привязка сторон, если отдали имена
        n1 = _g(payload, "team1Name", "t1Name", "teamOne")
        n2 = _g(payload, "team2Name", "t2Name", "teamTwo")
        if isinstance(n1, str) and n1:
            st.name1 = n1
        if isinstance(n2, str) and n2:
            st.name2 = n2

    def _apply(self, st: MatchState, key: str, payload):
        k = key.strip().lower()
        try:
            if k in ("roundend", "roundended"):
                self._on_round_end(st, payload)
            elif k in ("roundstart", "roundstarted"):
                st.bomb = ""
                st.round_no = st.ct_score + st.t_score + 1
                st._money_players = [{}, {}]   # новый раунд — новый замер денег
            elif k in ("bombplanted", "bombplant"):
                st.bomb = "заложена"
            elif k in ("bombdefused", "bombdefuse"):
                st.bomb = "разминирована"
            elif k in ("bombexploded", "bombexplode", "targetbombed"):
                st.bomb = "взрыв"
            elif k in ("mapchange", "mapchanged", "map"):
                mp = _g(payload, "map", "mapName", "name")
                if isinstance(mp, str) and mp:
                    st.map_name = mp
            elif k in ("matchstarted", "matchstart"):
                st.ct_score = st.t_score = 0
                st.last_rounds.clear()
            elif k in ("matchended", "matchend", "matchfinished"):
                st.match_over = True
            elif k in ("kill", "playerkilled", "death"):
                self._on_kill(st, payload)
            elif k in ("playersupdated", "playerupdated", "scoreupdated",
                       "scoreboardupdated", "economyupdated", "money"):
                self._on_players(st, payload)
            elif k in ("con_check", "concheck", "live", "token", "readyformatch"):
                pass   # служебные
            else:
                st.debug.append((time.time(), key, json.dumps(payload)[:220]))
        except Exception as e:
            log.debug("scorebot: парсинг %s: %s", key, e)
        st.touch()

    def _on_round_end(self, st: MatchState, p: dict):
        winner = _g(p, "winner", "winnerSide", "winningSide", "winnerTeam")
        wt = str(_g(p, "winType", "win_type") or "").strip().lower()
        cs = _g(p, "counterTerroristScore", "ctScore")
        ts = _g(p, "terroristScore", "tScore")
        side = ""
        if winner is not None:
            if str(winner) in ("0",):
                side = "T"
            elif str(winner) in ("1",):
                side = "CT"
            else:
                w = str(winner).upper()
                side = "CT" if "CT" in w else ("T" if w.endswith("T") or w == "T" else "")
        if isinstance(cs, (int, float)) and isinstance(ts, (int, float)):
            st.ct_score, st.t_score = int(cs), int(ts)
        elif side:
            if side == "CT":
                st.ct_score += 1
            else:
                st.t_score += 1
        st.flip_sides_if_needed()
        st.round_no = st.ct_score + st.t_score + 1
        if side:
            idx = st.winner_idx_of_side(side)
            st.last_rounds.append({"n": st.ct_score + st.t_score,
                                   "winner_idx": idx, "wt": _WIN_TYPES.get(wt, wt)})
            wname, cnt = st.streak
            st.streak = (idx, cnt + 1) if wname == idx else (idx, 1)
        st.bomb = ""

    def _on_kill(self, st: MatchState, p: dict):
        killer = _g(p, "killer", "attacker", "attackerName")
        victim = _g(p, "victim", "victimName")
        weapon = _g(p, "weapon", "weaponName") or ""
        hs = bool(_g(p, "headshot", "hs"))
        line = ""
        if isinstance(killer, dict) and isinstance(victim, dict):
            line = (f"{_player_name(killer)} [{weapon}] → {_player_name(victim)}"
                    + (" 🔫HS" if hs else ""))
            self._absorb_money(st, killer)
            self._absorb_money(st, victim)
        elif isinstance(victim, dict):
            line = f"{weapon or '?'} → {_player_name(victim)}"
        if line:
            st.kills.append(line)

    def _absorb_money(self, st: MatchState, p: dict):
        """Деньги игрока из события -> в сумму команды (если понятно, за кого)."""
        money = _g(p, "money", "cash", "playerMoney")
        if not isinstance(money, (int, float)) or money < 0:
            return
        team = _g(p, "team", "side", "teamIdx", "teamIndex")
        idx = None
        if team is not None:
            t = str(team).upper()
            if t in ("0", "T", "TERRORIST"):
                idx = st.winner_idx_of_side("T")
            elif t in ("1", "CT", "COUNTER-TERRORIST", "COUNTERTERRORIST"):
                idx = st.winner_idx_of_side("CT")
        else:
            nm = _player_name(p).strip().lower()
            in1, in2 = nm in st.players1, nm in st.players2
            if in1 and not in2:
                idx = 0
            elif in2 and not in1:
                idx = 1
        if idx is None:
            return
        # пересобираем грубую сумму: копим максимум на игрока в рамках раунда
        if not hasattr(st, "_money_players"):
            st._money_players = [{}, {}]   # name -> max money за раунд
        slot = st._money_players[idx]
        nm = _player_name(p)
        slot[nm] = max(slot.get(nm, 0), int(money))
        st.money[idx] = sum(slot.values())

    def _on_players(self, st: MatchState, p: dict):
        """Обновления состава/счёта: пробуем вытащить деньги и счёт."""
        cs = _g(p, "counterTerroristScore", "ctScore")
        ts = _g(p, "terroristScore", "tScore")
        if isinstance(cs, (int, float)) and isinstance(ts, (int, float)):
            st.ct_score, st.t_score = int(cs), int(ts)
        players = _g(p, "players", "playerStates", "allPlayers")
        if isinstance(players, list):
            for pl in players:
                if isinstance(pl, dict):
                    self._absorb_money(st, pl)

    # ---------- сервис ----------

    async def _watchdog_loop(self):
        while True:
            await asyncio.sleep(30)
            # V112fix: здоровье страницы-хоста. Синглтон браузера мог
            # перезапуститься (страница моста умерла вместе с ним) — тогда
            # сбрасываем, чтобы следующая подписка пересоздала мост.
            if self._page is not None:
                try:
                    if self._page.is_closed():
                        log.warning("scorebot: страница-мост закрыта — "
                                    "перезапуск при следующей подписке")
                        self._page = None
                        self._js_ready = False
                except Exception:
                    pass
            now = time.time()
            for mid, st in list(self._states.items()):
                if not st.got_data and now - st.subscribed_at > 120:
                    if not st.broken:
                        st.broken = True
                        log.warning("scorebot %s: данных нет 2 мин — режим опроса",
                                    mid)
                if st.got_data and now - st.last_change > 180:
                    log.info("scorebot %s: тишина 3 мин (матч окончен?)", mid)


bridge = ScorebotBridge()


# ================================================================ карточка

def format_card(st: MatchState) -> str:
    """Лайв-карточка для Telegram (HTML)."""
    s1, s2 = st.scores()
    a1, a2 = st.avgs()
    lines = [f"🔴 <b>ЛАЙВ-ЦЕНТР</b> · {st.name1} vs {st.name2}"]
    if st.map_name:
        lines.append(f"🗺 {st.map_name} · раунд <b>{st.round_no}</b>")
    else:
        lines.append(f"Раунд <b>{st.round_no}</b>")
    lines.append(f"Счёт: <b>{s1}:{s2}</b> "
                 f"(столбцы: {st.side1}/{'T' if st.side1 == 'CT' else 'CT'})")
    if a1 is not None or a2 is not None:
        lines.append("")
        if a1 is not None:
            lines.append(f"💰 {st.name1}: ≈{a1:,.0f}$ на игрока · "
                         f"<b>{buy_label(a1)}</b>")
        else:
            lines.append(f"💰 {st.name1}: данных нет")
        if a2 is not None:
            lines.append(f"💰 {st.name2}: ≈{a2:,.0f}$ на игрока · "
                         f"<b>{buy_label(a2)}</b>")
        else:
            lines.append(f"💰 {st.name2}: данных нет")
    if st.bomb:
        lines.append(f"💣 Бомба: {st.bomb}")
    wname, cnt = st.streak
    if cnt >= 2 and wname is not None:
        who = st.name1 if wname == 0 else st.name2
        lines.append(f"🔥 Серия: {who} взял(а) {cnt} раунда подряд")
    if st.last_rounds:
        parts = []
        for r in list(st.last_rounds)[-4:]:
            w = st.name1 if r["winner_idx"] == 0 else st.name2
            parts.append(f"#{r['n']} {w} ({r['wt'] or '?'})")
        lines.append("Раунды: " + " · ".join(parts))
    if st.kills:
        lines.append("")
        for kl in list(st.kills)[-3:]:
            lines.append(f"• {kl}")
    src = "HLTV scorebot" if st.got_data else "ждём данные HLTV…"
    lines.append("")
    lines.append(f"<i>{src} · {_now_msk()}</i>")
    return "\n".join(lines)[:3800]
