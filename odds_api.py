"""
odds_api.py — коэффициенты российских БК (Этап 2).

Как добываем (проверено на живых линиях, октябрь 2026):
  • ПАРИ    — прямой HTTP: line-lb51/lb01/vk01.pb06e2-resources.com/events/list
              (тот же JSON, что сайт pari.ru качает себе сам; браузер не нужен).
              Кэфы: фактор 921 = П1, 923 = П2. Требует российский IP.
  • ФОНБЕТ  — прямой HTTP: line-hz.bkfon-resources.com (events/list, запасной
              currentLine). Формат той же семьи, что у Пари: 921 = П1, 923 = П2.
              Тоже только с российских IP.
  • ВИНЛАЙН — линия гоняется через бинарный websocket, поэтому открываем
              страницу киберлинии в Camoufox и снимаем кефы с отрисованных
              карточек (div.card -> блок с подписью «Матч»).
  • БЕТБУМ  — сайт за антиботом QRator. Открываем страницу браузером,
              перехватываем её же API-ответы + снимаем DOM. Схема может
              меняться — всё непонятое складывается в debug_odds/*.json,
              по дампам парсер докручивается за минуты.

Сопоставление команд: norm_name() + алиасы (NaVI = Natus Vincere и т.д.).
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from config import BASE_DIR, ODDS_SITES

log = logging.getLogger("odds")

DEBUG_DIR = BASE_DIR / "debug_odds"
DEBUG_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MIN_ODD, MAX_ODD = 1.01, 60.0

# ================================================================ имена

_TOKEN_JUNK = {"team", "esports", "esport", "gaming", "gg", "club", "cs", "cs2",
               "cyber", "cyberathlete", "pro", "the", "e"}

ALIASES = {
    "navi": ["natus vincere", "navi junior", "na vi"],
    "virtus.pro": ["virtus pro", "vp"],
    "g2": ["g2 esports"],
    "fut": ["fut esports"],
    "spirit": ["team spirit"],
    "liquid": ["team liquid"],
    "vitality": ["team vitality"],
    "mouz": ["mousesports", "mouz nxg"],
    "big": ["big clan", "berlin international gaming"],
    "coL": ["compLexity"],
    "falcons": ["team falcons"],
    "astralis": ["astralis talent"],
    "og": ["og esports"],
    "cloud9": ["cloud 9", "c9"],
    "fnatic": ["fnatic cs"],
    "nexus": ["nexus gaming"],
    "sangal": ["sangal esports"],
    "forze": ["forze esports", "forze gaming"],
    "koi": ["koi gaming"],
    "9ine": ["9ine team"],
    "eternal fire": ["eternalfire"],
    "monte": ["monte esports"],
    "gl": ["gamerlegion", "gamer legion"],
    "b8": ["b8 team"],
    "apex": ["apex gaming"],
}


def norm_name(s: str) -> str:
    """Приводим название команды к сопоставимому виду."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9 ]", " ", s)
    toks = [t for t in s.split() if t and t not in _TOKEN_JUNK]
    return " ".join(toks)


def names_match(a: str, b: str) -> bool:
    """HLTV-имя и БК-имя — это одна команда?"""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    ta, tb = na.split()[-1], nb.split()[-1]
    if ta == tb and len(ta) >= 3:
        return True
    for key, variants in ALIASES.items():
        group = {norm_name(key)} | {norm_name(v) for v in variants}
        if na in group and nb in group:
            return True
    return False


def pair_matches(bk_t1: str, bk_t2: str, name1: str, name2: str) -> bool:
    """Матч целиком (порядок сторон может отличаться)."""
    return ((names_match(bk_t1, name1) and names_match(bk_t2, name2)) or
            (names_match(bk_t1, name2) and names_match(bk_t2, name1)))


def _ok_odd(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return MIN_ODD <= f <= MAX_ODD


def _dump(name: str, url: str, data, note: str = ""):
    """Сохраняем неразобранное — по дампам докручиваем парсеры."""
    try:
        stamp = datetime.now().strftime("%H%M%S")
        f = DEBUG_DIR / f"{name}_{stamp}.json"
        f.write_text(json.dumps({"url": url, "note": note, "data": data},
                                ensure_ascii=False)[:4_000_000], encoding="utf-8")
        log.info("Дамп сохранён: %s", f.name)
    except Exception:
        pass


def _http_json(url: str, referer: str = "", timeout: int = 15):
    """GET -> JSON (requests сам распаковывает gzip). None при неудаче."""
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "ru-RU,ru;q=0.9"}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        log.debug("HTTP %s %s", r.status_code, url[:100])
        return None
    return r.json()


# ================================================================ ПАРИ (HTTP)

PARI_HOSTS = [
    "https://line-lb51-w.pb06e2-resources.com",
    "https://line-lb01-w.pb06e2-resources.com",
    "https://line-vk01-w.pb06e2-resources.ru",
]
# факторы: 921 = П1, 922 = ничья, 923 = П2 (каталог factorsCatalog/tables)
PARI_W1, PARI_W2 = 921, 923


def _is_cs_segment(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n:
        return False
    if "сравнен" in n or "статистик" in n:
        return False
    return (n.startswith("counter-strike") or n.startswith("counter strike")
            or n.startswith("cs2") or n.startswith("cs:go") or n.startswith("csgo"))


def fetch_pari_sync() -> list[dict]:
    """Пари: /events/list — sports + events + customFactors одним пакетом."""
    for host in PARI_HOSTS:
        url = f"{host}/events/list?version=0&lang=ru&scopeMarket=2300"
        try:
            data = _http_json(url, referer="https://pari.ru/")
            if not isinstance(data, dict) or "events" not in data:
                _dump("pari", url, str(data)[:2000], note="не похоже на events/list")
                continue
            sports = {s.get("id"): s for s in data.get("sports", []) if isinstance(s, dict)}
            cs_ids = {sid for sid, s in sports.items() if _is_cs_segment(s.get("name", ""))}
            if not cs_ids:
                _dump("pari", url, {"sports_sample": data.get("sports", [])[:40]},
                      note="не нашли CS-сегменты")
                continue
            now = time.time()
            out, seen = [], set()
            for ev in data.get("events", []):
                if not isinstance(ev, dict):
                    continue
                if ev.get("sportId") not in cs_ids:
                    continue
                # level 1 + kind 1 = исход целого матча (не «2-я карта» и не дуэли)
                if ev.get("level") not in (1, "1") or ev.get("kind") not in (1, "1"):
                    continue
                t1, t2 = (ev.get("team1") or "").strip(), (ev.get("team2") or "").strip()
                if not t1 or not t2:
                    continue
                st = ev.get("startTime")
                if isinstance(st, (int, float)):
                    if st > 10**12:      # миллисекунды -> секунды
                        st /= 1000.0
                    # старт в прошлом больше чем на час — пропускаем (уже идёт/закончился)
                    if st < now - 3600:
                        continue
                w1 = w2 = None
                for cf in data.get("customFactors", []):
                    if not isinstance(cf, dict) or cf.get("e") != ev.get("id"):
                        continue
                    for f in cf.get("factors", []) or []:
                        fid = f.get("f") if isinstance(f, dict) else None
                        if fid == PARI_W1:
                            w1 = f.get("v")
                        elif fid == PARI_W2:
                            w2 = f.get("v")
                    break
                if _ok_odd(w1) and _ok_odd(w2):
                    key = (norm_name(t1), norm_name(t2))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"team1": t1, "team2": t2, "w1": float(w1),
                                "w2": float(w2), "source": "pari",
                                "start": int(st) if isinstance(st, (int, float)) else None})
            if out:
                log.info("Пари: %s CS2-матчей (host=%s)", len(out), host)
                return out
            log.debug("Пари %s: пакет получен, но CS2-пар с кефами 0", host)
        except Exception as e:
            log.debug("pari %s: %s", host, e)
    log.warning("Пари: линия недоступна (нужен российский IP?)")
    return []


async def fetch_pari(page=None) -> list[dict]:
    return await asyncio.to_thread(fetch_pari_sync)


# ================================================================ ФОНБЕТ (HTTP)

FONBET_HOSTS = [
    "https://line-hz.bkfon-resources.com",
    "https://line-fb2.bkfon-resources.com",
    "https://line-51.bkfon-resources.com",
    "https://line-52.bkfon-resources.com",
]
FONBET_W1, FONBET_W2 = 921, 923  # у Фонбета и Пари одна факторная база


def _fonbet_cs_ids(sports: list) -> set:
    """Дисциплины CS: сегменты «Counter-Strike …» (+ родитель «Киберспорт»)."""
    ids = set()
    for s in sports:
        if not isinstance(s, dict):
            continue
        nm = str(s.get("name", "")).lower()
        if _is_cs_segment(nm):
            ids.add(s.get("id"))
    return ids


def _parse_fonbet_factors(data: dict) -> dict[int, dict[int, float]]:
    """
    customFactors бывает двух видов:
      A) списки-тройки [фактор, событие, кеф] в любом порядке позиций
      B) dicts как у Пари: {e: id, factors: [{f, v}]}
    Возврат: {event_id: {factor_id: value}}
    """
    res: dict[int, dict[int, float]] = {}
    for cf in data.get("customFactors", []) or []:
        if isinstance(cf, dict) and "e" in cf:                     # вид B
            facs = res.setdefault(cf.get("e"), {})
            for f in cf.get("factors", []) or []:
                if isinstance(f, dict) and "f" in f and "v" in f:
                    facs[int(f["f"])] = float(f["v"])
        elif isinstance(cf, (list, tuple)) and len(cf) >= 3:       # вид A
            a, b, c = cf[0], cf[1], cf[2]
            fa = isinstance(a, (int, float)) and 900 <= a <= 999
            fc = isinstance(c, (int, float)) and 900 <= c <= 999
            if fa and _ok_odd(c) and isinstance(b, (int, float)):
                res.setdefault(int(b), {})[int(a)] = float(c)      # [f, e, v]
            elif fc and _ok_odd(a) and isinstance(b, (int, float)):
                res.setdefault(int(b), {})[int(c)] = float(a)      # [v, e, f]
    return res


def fetch_fonbet_sync() -> list[dict]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json",
                         "Accept-Language": "ru-RU,ru;q=0.9"})
    now = time.time()
    for host in FONBET_HOSTS:
        for path in ("/events/list?lang=ru&scopeMarket=2600&version=0",
                     "/events/list?lang=ru&scopeMarket=2300&version=0",
                     "/line/currentLine/ru/"):
            url = host + path
            try:
                r = sess.get(url, timeout=12)
                if r.status_code != 200:
                    continue
                data = r.json()
                if not isinstance(data, dict) or "events" not in data:
                    _dump("fonbet", url, str(data)[:2000], note="не events-структура")
                    continue
                cs_ids = _fonbet_cs_ids(data.get("sports", []))
                if not cs_ids:
                    _dump("fonbet", url, {"sports_sample": data.get("sports", [])[:40]},
                          note="не нашли CS-спорты")
                    continue
                factors = _parse_fonbet_factors(data)
                out, seen = [], set()
                for ev in data.get("events", []):
                    if not isinstance(ev, dict) or ev.get("sportId") not in cs_ids:
                        continue
                    t1, t2 = (ev.get("team1") or "").strip(), (ev.get("team2") or "").strip()
                    if not t1 or not t2:
                        continue
                    # пропускаем дочерние события («2-я карта»): у них нет имён команд
                    if ev.get("level") not in (1, "1", None):
                        continue
                    st = ev.get("startTime")
                    if isinstance(st, (int, float)):
                        if st > 10**12:
                            st /= 1000.0
                        if st < now - 3600:
                            continue
                    facs = factors.get(ev.get("id"), {})
                    w1, w2 = facs.get(FONBET_W1), facs.get(FONBET_W2)
                    if _ok_odd(w1) and _ok_odd(w2):
                        key = (norm_name(t1), norm_name(t2))
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({"team1": t1, "team2": t2, "w1": float(w1),
                                    "w2": float(w2), "source": "fonbet",
                                    "start": int(st) if isinstance(st, (int, float)) else None})
                if out:
                    log.info("Фонбет: %s CS2-матчей (host=%s path=%s)", len(out), host, path[:30])
                    return out
            except Exception as e:
                log.debug("fonbet %s: %s", host, e)
    log.warning("Фонбет: ни один хост не ответил (нужен российский IP?)")
    return []


async def fetch_fonbet() -> list[dict]:
    return await asyncio.to_thread(fetch_fonbet_sync)


# ================================================================ ВИНЛАЙН (DOM)

_WINLINE_JS = """
() => {
    const res = [];
    const cards = document.querySelectorAll('div.card');
    for (const card of cards) {
        try {
            const names = [...card.querySelectorAll('.body-left__names .name')]
                .map(e => (e.textContent || '').trim()).filter(Boolean);
            if (names.length < 2) continue;
            // турнир — ближайший блок чемпионата
            let tour = '';
            const champ = card.closest('[class*="champ"]');
            if (champ) {
                tour = (champ.innerText || '').split('\\n')[0].trim();
            }
            const a = card.querySelector('a[href*="/stavki/event/"]');
            const href = a ? a.getAttribute('href') : '';
            // первый блок .card__body с подписью «Матч»
            let w1 = null, w2 = null;
            for (const body of card.querySelectorAll('.card__body')) {
                const label = body.querySelector('.match-row-label, .period-name');
                const lt = ((label && label.textContent) || '').trim().toLowerCase();
                if (!lt.startsWith('матч')) continue;
                const market = body.querySelector('.card__market');
                if (!market) break;
                const nums = [...market.querySelectorAll('.coefficient-button span')]
                    .map(s => parseFloat((s.textContent || '').replace(',', '.')))
                    .filter(v => isFinite(v) && v > 1.005 && v < 60);
                if (nums.length >= 2) { w1 = nums[0]; w2 = nums[1]; }
                break;
            }
            if (!w1 || !w2) continue;
            res.push({t1: names[0], t2: names[1], w1, w2,
                      live: card.className.includes('--live'), tour, href});
        } catch (e) {}
    }
    return res;
}
"""


async def fetch_winline(page) -> list[dict]:
    urls = ["https://winline.ru/stavki/sport/kibersport"] + \
           ODDS_SITES.get("winline", {}).get("urls", [])
    seen_urls = set()
    for url in urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log.debug("winline goto %s: %s", url, e)
            continue
        # ждём карточки
        try:
            await page.wait_for_selector("div.card", timeout=20000)
        except Exception:
            pass
        # докручиваем — подгружаются ещё матчи
        try:
            for _ in range(3):
                await page.mouse.wheel(0, 1600)
                await page.wait_for_timeout(1800)
        except Exception:
            pass
        try:
            rows = await page.evaluate(_WINLINE_JS)
        except Exception as e:
            log.debug("winline evaluate: %s", e)
            rows = []
        pairs = []
        for r in rows or []:
            tour = (r.get("tour") or "")
            if not _is_cs_segment(tour.replace(" | ", ". ")):
                continue
            pairs.append({"team1": r["t1"], "team2": r["t2"],
                          "w1": float(r["w1"]), "w2": float(r["w2"]),
                          "source": "winline", "live": bool(r.get("live"))})
        if pairs:
            log.info("Винлайн: %s CS2-карточек (%s)", len(pairs), url)
            return pairs
        try:
            html = await page.content()
            _dump("winline", url, html[:200000], note="page html, карточек с кефами 0")
        except Exception:
            pass
    log.warning("Винлайн: карточек с кефами не найдено")
    return []


# ================================================================ БЕТБУМ (браузер)

_BB_EXCLUDE = ("edna", "qrator", "qauth", "/api/fl", "chat", "analytics",
               "static", "assets", "fonts", "tracker", "metric")


async def fetch_betboom(page) -> list[dict]:
    """Бетбум: страница за QRator — открываем браузером, ловим её же JSON
    и снимаем DOM. Всё непонятое — в debug_odds (по дампам докрутим)."""
    urls = ODDS_SITES.get("betboom", {}).get("urls", [])
    for url in urls:
        payloads: list[tuple[str, object]] = []
        try:
            async def on_response(resp):
                try:
                    u = str(resp.url)
                    if "betboom" not in u:
                        return
                    if any(x in u for x in _BB_EXCLUDE):
                        return
                    ct = (resp.headers or {}).get("content-type", "")
                    if "json" not in ct:
                        return
                    body = await resp.json()
                    payloads.append((u, body))
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(14000)
                try:
                    await page.mouse.wheel(0, 2000)
                    await page.wait_for_timeout(4000)
                except Exception:
                    pass
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
        except Exception as e:
            log.debug("betboom goto %s: %s", url, e)
            continue

        found = []
        for u, body in payloads:
            try:
                got = extract_pairs(body, source="betboom")
            except Exception:
                got = []
            if got:
                found.extend(got)
            else:
                _dump("betboom", u, body if not isinstance(body, str) else body[:100000])
        if found:
            # только CS2-пары отфильтруются на стороне сопоставления имён
            log.info("Бетбум: %s пар (%s)", len(found), url)
            return found[:200]
        # DOM-фолбэк: сканируем весь видимый текст на пары «имя/имя/кеф/кеф»
        try:
            dom_rows = await page.evaluate("""
                () => {
                    const out = [];
                    const seen = new Set();
                    for (const el of document.querySelectorAll('div, li, a')) {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 300 || t.split('\\n').length < 4) continue;
                        const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                        const nums = lines.filter(l => /^\\d+[.,]\\d{2}$/.test(l));
                        if (nums.length < 2) continue;
                        const words = lines.filter(l => /^[A-Za-z0-9\\ |.-]{2,40}$/.test(l)
                                                       && !/^\\d/.test(l) && l !== l.toLowerCase());
                        if (words.length < 2) continue;
                        const key = words.slice(0, 2).join('|');
                        if (seen.has(key)) continue;
                        seen.add(key);
                        out.push({t1: words[0], t2: words[1],
                                  w1: parseFloat(nums[0].replace(',', '.')),
                                  w2: parseFloat(nums[1].replace(',', '.'))});
                    }
                    return out.slice(0, 100);
                }
            """)
            for r in dom_rows or []:
                if _ok_odd(r.get("w1")) and _ok_odd(r.get("w2")):
                    found.append({"team1": r["t1"], "team2": r["t2"],
                                  "w1": float(r["w1"]), "w2": float(r["w2"]),
                                  "source": "betboom"})
            if found:
                log.info("Бетбум DOM: %s пар (%s)", len(found), url)
                return found[:200]
        except Exception as e:
            log.debug("betboom dom: %s", e)
    log.warning("Бетбум: данных не добыто (дампы в debug_odds)")
    return []


# ================================================================ generic-извлекатель

_ODD_KEYS = ("coef", "coefficient", "odd", "rate", "price", "k1", "k2", "w1", "w2")
_NAME_KEYS = ("participant", "team", "opponent", "member", "home", "away", "player",
              "competitor", "side")


def _is_team_name(s) -> bool:
    return (isinstance(s, str) and 2 <= len(s) <= 40 and not s.isdigit()
            and not s.startswith("http"))


def extract_pairs(obj, source: str = "", depth: int = 0):
    """Достаёт из произвольного JSON пары «команды + кефы» (для Бетбума)."""
    out: list[dict] = []
    seen = set()

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def walk(o):
        if isinstance(o, dict):
            p1 = o.get("participant1") or o.get("participant1_name")
            p2 = o.get("participant2") or o.get("participant2_name")
            if _is_team_name(p1) and _is_team_name(p2):
                w1 = w2 = None
                ml = o.get("mainLine") or {}
                v = ml.get("V") if isinstance(ml, dict) else None
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    w1, w2 = _num(v[0]), _num(v[1])
                if not (w1 and w2):
                    for k, val in o.items():
                        kl = k.lower()
                        if w1 is None and _ok_odd(val) and "1" in kl and any(x in kl for x in _ODD_KEYS):
                            w1 = _num(val)
                        if w2 is None and _ok_odd(val) and "2" in kl and any(x in kl for x in _ODD_KEYS):
                            w2 = _num(val)
                if w1 and w2:
                    key = (str(p1), str(p2), round(w1, 3), round(w2, 3))
                    if key not in seen:
                        seen.add(key)
                        out.append({"team1": str(p1), "team2": str(p2),
                                    "w1": float(w1), "w2": float(w2), "source": source})
                for vv in o.values():
                    walk(vv)
                return
            n1 = n2 = w1 = w2 = None
            for k, val in o.items():
                kl = k.lower()
                if _is_team_name(val):
                    for nk in _NAME_KEYS:
                        if kl.startswith(nk):
                            if kl.endswith(("1", "a", "home", "l")) and n1 is None:
                                n1 = val
                            elif kl.endswith(("2", "b", "away", "r", "w")) and n2 is None:
                                n2 = val
                            break
                if _looks_odd_num(val):
                    for ok_ in ("w1", "k1", "coef1", "odd1", "rate1", "price1"):
                        if kl == ok_ and w1 is None:
                            w1 = _num(val)
                    for ok_ in ("w2", "k2", "coef2", "odd2", "rate2", "price2"):
                        if kl == ok_ and w2 is None:
                            w2 = _num(val)
            if n1 and n2 and w1 and w2:
                key = (str(n1), str(n2), round(w1, 3), round(w2, 3))
                if key not in seen:
                    seen.add(key)
                    out.append({"team1": str(n1), "team2": str(n2),
                                "w1": float(w1), "w2": float(w2), "source": source})
            for vv in o.values():
                walk(vv)
        elif isinstance(o, list):
            for vv in o:
                walk(vv)

    walk(obj)
    return out


def _looks_odd_num(v) -> bool:
    return isinstance(v, (int, float)) and MIN_ODD <= float(v) <= MAX_ODD


# ================================================================ сводка

async def collect_all_odds(page=None, status_cb=None) -> dict[tuple, dict]:
    """
    Собирает кефы со всех включённых БК.
    Возврат: {(norm1, norm2): {bookmaker: {team1, team2, w1, w2}}}
    """
    from config import ODDS_ENABLED
    bookmakers: dict[tuple, dict] = {}

    async def add(bk: str, pairs: list[dict]):
        if status_cb:
            await status_cb(f"{bk}: {len(pairs)} матчей")
        for p in pairs:
            a, b = norm_name(p["team1"]), norm_name(p["team2"])
            if not a or not b or a == b:
                continue
            key = tuple(sorted((a, b)))
            bookmakers.setdefault(key, {})[bk] = p

    if "pari" in ODDS_ENABLED:
        try:
            await add("pari", await fetch_pari())
        except Exception as e:
            log.warning("pari: %s", e)
    if "fonbet" in ODDS_ENABLED:
        try:
            await add("fonbet", await fetch_fonbet())
        except Exception as e:
            log.warning("fonbet: %s", e)

    if page is not None and any(bk in ODDS_ENABLED for bk in ("winline", "betboom")):
        for bk, fn in (("winline", fetch_winline), ("betboom", fetch_betboom)):
            if bk not in ODDS_ENABLED:
                continue
            try:
                await add(bk, await fn(page))
            except Exception as e:
                log.warning("%s: %s", bk, e)

    log.info("Итог кефов: %s уникальных пар", len(bookmakers))
    return bookmakers


def find_odds_for(bookmakers: dict, name1: str, name2: str) -> dict[str, dict] | None:
    """Ищет кефы конкретной пары HLTV-команд среди собранных."""
    for key, per_bk in bookmakers.items():
        a, b = key
        if (names_match(a, name1) and names_match(b, name2)) or \
           (names_match(a, name2) and names_match(b, name1)):
            return per_bk
    return None


def align_odds(raw: dict, name1: str, name2: str) -> dict[str, tuple[float, float]]:
    """
    Приводит кефы из БД к виду {bookmaker: (кеф на name1, кеф на name2)}.
    Учитывает сохранённые стороны: если БК записал команды в обратном порядке —
    переворачиваем, чтобы «П1» всегда означала name1.
    """
    out = {}
    for bk, d in (raw or {}).items():
        w1, w2 = d.get("w1"), d.get("w2")
        if not w1 or not w2:
            continue
        sides = d.get("sides") or []
        if len(sides) == 2:
            s1_matches_n1 = names_match(sides[0], name1)
            s2_matches_n1 = names_match(sides[1], name1)
            if s2_matches_n1 and not s1_matches_n1:
                out[bk] = (w2, w1)
                continue
        out[bk] = (w1, w2)
    return out
