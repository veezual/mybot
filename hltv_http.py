"""
hltv_http.py — V106: лёгкий доступ к HLTV БЕЗ браузера (обычный aiohttp).

Идея: с домашнего (резидентного) IP HLTV в большинстве случаев отдаёт
страницы обычным HTTP-запросом, без Cloudflare-проверки. Это занимает
1-3 секунды вместо 30-120 секунд в Camoufox.

Если Cloudflare всё же преградил (403/челлендж) — кидаем RuntimeError,
и цепочка в collector.py переходит к следующему уровню (Camoufox).
Бот НИКОГДА не падает из-за этого модуля.

Селекторы парсинга те же, что в hltv_api.py (match-wrapper,
map-pool-map-name, result-con) — проверены на реальных страницах.
"""
import asyncio
import logging
import re
from datetime import datetime
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup

from config import HLTV_BASE, TZ_NAME

log = logging.getLogger("hltv_http")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": f"{HLTV_BASE}/",
}

TIMEOUT = aiohttp.ClientTimeout(total=15)

# признаки страницы-заглушки Cloudflare
_CF_MARKERS = ("Just a moment", "challenge-platform", "cf-chl",
               "cf-browser-verification", "Attention Required", "moment...")


def _fmt_date_from_unix(ms: str | None) -> str:
    try:
        ts = int(ms) / 1000
        return datetime.fromtimestamp(ts, tz=ZoneInfo(TZ_NAME)).strftime("%d.%m.%y")
    except Exception:
        return ""


async def _fetch_html(path: str, marker: str) -> str:
    """GET страницы HLTV; ошибки и Cloudflare -> RuntimeError."""
    url = f"{HLTV_BASE}{path}"
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status != 200:
                raise RuntimeError(f"HLTV {path} -> HTTP {r.status}")
            html = await r.text()
    if len(html) < 500 or any(m in html for m in _CF_MARKERS):
        raise RuntimeError("HLTV отдал страницу Cloudflare")
    if marker and marker not in html:
        raise RuntimeError(f"HLTV {path}: нет маркера '{marker}'")
    return html


# ---------------------------------------------------------------- матчи

async def fetch_today_matches_http() -> list[dict]:
    """Список матчей дня со страницы /matches. Формат как у hltv_api."""
    html = await _fetch_html("/matches", "match-wrapper")
    soup = BeautifulSoup(html, "html.parser")

    matches, seen = [], set()
    for mw in soup.find_all("div", class_="match-wrapper"):
        t1_id = mw.get("team1")
        t2_id = mw.get("team2")
        if not t1_id or not t2_id:
            continue

        blocks = mw.find_all("div", class_="match-team")
        if len(blocks) < 2:
            continue

        names, logos = [], []
        for blk in blocks:
            name = ""
            name_el = blk.find("div", class_="match-teamname")
            if name_el:
                name = name_el.get_text(strip=True)
            if not name:
                img = blk.find("img")
                if img and img.get("alt"):
                    name = img["alt"]
            name = re.sub(r"\s+\d+$", "", name).strip()

            logo = ""
            img = blk.find("img")
            if img and img.get("src"):
                logo = urljoin(HLTV_BASE, img["src"])
            names.append(name)
            logos.append(logo)

        start_time = ""
        time_el = mw.find(class_="match-time") or mw.find(class_="matchTime")
        if time_el:
            start_time = time_el.get_text(strip=True)
        if not start_time:
            start_time = _fmt_date_from_unix(mw.get("data-unix"))

        if not names[0] or not names[1]:
            continue

        # V111: id страницы матча (для Лайв-центра / scorebot)
        match_id = ""
        for a in mw.find_all("a", href=True):
            m = _MATCH_ID_RE.search(a["href"])
            if m:
                match_id = m.group(1)
                break

        key = (t1_id, t2_id)
        if key in seen:
            continue
        seen.add(key)

        matches.append({
            "team1_id": t1_id, "team1_name": names[0], "team1_logo": logos[0],
            "team2_id": t2_id, "team2_name": names[1], "team2_logo": logos[1],
            "start_time": start_time,
            "match_id": match_id,
        })

    log.info("HLTV HTTP: матчей найдено %s", len(matches))
    return matches


# ---------------------------------------------------------------- карты

_MAP_RE = re.compile(r"^(.*?)\s*[-–—]?\s*(\d+(?:\.\d+)?)\s*%\s*$")
_MATCH_ID_RE = re.compile(r"/matches/(\d{4,})")   # V111: id страницы матча (scorebot)


async def fetch_team_maps_http(team_id: str, team_name: str) -> list[tuple[str, float]]:
    """Винрейты карт со страницы stats. Формат как у hltv_api: [(карта, %), ...]."""
    html = await _fetch_html(f"/stats/teams/maps/{team_id}/{quote(team_name)}",
                             "map-pool-map-name")
    soup = BeautifulSoup(html, "html.parser")

    result: dict[str, float] = {}
    for div in soup.select(".map-pool-map-name"):
        text = div.get_text(strip=True)
        m = _MAP_RE.match(text)
        if m:
            result.setdefault(m.group(1).strip(" -–—"), float(m.group(2)))

    if not result:  # запасной путь: по тексту страницы
        for m in re.finditer(r"([A-Za-z0-9 _\-]+?)\s*[-–—]\s*(\d+(?:\.\d+)?)%", html):
            result.setdefault(m.group(1).strip(), float(m.group(2)))

    maps = sorted(result.items(), key=lambda x: -x[1])
    log.info("HLTV HTTP: карты %s (%s): %s шт.", team_name, team_id, len(maps))
    if not maps:
        raise RuntimeError("карты не найдены")
    return maps


# ---------------------------------------------------------------- H2H

async def fetch_h2h_http(id1: str, id2: str) -> list[dict]:
    """Личные встречи: /results?team=X&team=Y. Формат как у hltv_api."""
    html = await _fetch_html(f"/results?team={id1}&team={id2}", "result-con")
    soup = BeautifulSoup(html, "html.parser")

    rows, seen = [], set()
    for row in soup.select(".result-con"):
        teams = row.select(".team")
        if len(teams) != 2:
            continue
        t1 = teams[0].get_text(strip=True)
        t2 = teams[1].get_text(strip=True)
        score_el = row.select_one(".result-score")
        score = score_el.get_text(strip=True) if score_el else "?"
        event_el = row.select_one(".event-name")
        event = event_el.get_text(strip=True) if event_el else ""
        date_el = row.select_one(".date-cell") or row.select_one(".event-time")
        date = date_el.get_text(strip=True) if date_el else ""
        if not date:
            unix_el = row.select_one("[data-unix]")
            date = _fmt_date_from_unix(unix_el.get("data-unix")) if unix_el else ""
        key = (date, t1, t2, score, event)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"date": date, "t1": t1, "t2": t2, "score": score, "event": event})

    log.info("HLTV HTTP: H2H %s vs %s: %s матчей", id1, id2, len(rows))
    if not rows:
        raise RuntimeError("встреч не найдено")
    return rows


# ------------------------------------------------------- V115: форма команды

def parse_results_form(html: str, team_name: str, n: int = 10) -> dict:
    """V115: парс формы из HTML страницы /results?team=<id> (общий для
    HTTP- и браузерного путей). Формат: {wins, total, streak, streak_won, last}.
    Кидает RuntimeError, если ни одного матча не опознано."""
    from odds_api import names_match  # локально: избегаем цикла импортов

    soup = BeautifulSoup(html, "html.parser")
    results: list[int] = []
    for row in soup.select(".result-con"):
        if len(results) >= n:
            break
        teams = row.select(".team")
        if len(teams) != 2:
            continue
        t1 = teams[0].get_text(strip=True)
        t2 = teams[1].get_text(strip=True)
        score_el = row.select_one(".result-score")
        nums = re.findall(r"\d+", score_el.get_text(" ", strip=True)) if score_el else []
        if len(nums) < 2:
            continue  # техпоражение/кривой счёт
        s1, s2 = int(nums[0]), int(nums[1])
        if names_match(t1, team_name):
            won, played = s1 > s2, s1 != s2
        elif names_match(t2, team_name):
            won, played = s2 > s1, s1 != s2
        else:
            continue  # наша команда в этой строке не опознана
        if not played:
            continue
        results.append(1 if won else 0)

    if not results:
        raise RuntimeError("результатов команды не найдено")
    streak = 0
    for r in results:
        if r == results[0]:
            streak += 1
        else:
            break
    return {"wins": sum(results), "total": len(results),
            "streak": streak, "streak_won": bool(results[0]),
            "last": results}


async def fetch_team_form_http(team_id: str, team_name: str, n: int = 10) -> dict:
    """V115: форма команды (последние n матчей) со страницы результатов HLTV.

    Нужна как фолбэк для команд, которых bo3.gg не знает или у которых там
    пусто (свежие теги: Arena Pro, Tactical Chaos и т.п.).
    """
    html = await _fetch_html(f"/results?team={team_id}", "result-con")
    form = parse_results_form(html, team_name, n)
    log.info("HLTV HTTP: форма %s (%s): %d/%d", team_name, team_id,
             form["wins"], form["total"])
    return form
