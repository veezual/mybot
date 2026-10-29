"""
hltv_api.py — парсеры HLTV (работают через переданную playwright-страницу Camoufox).

Селекторы выверены на реальных страницах в V99 (debug-скрипты + содержимое базы):
  • матчи:        div.match-wrapper (атрибуты team1/team2) + .match-teamname / img[alt]
  • карты:        .map-pool-map-name  →  "Ancient - 70.7%"
  • личные встречи: /results?team=X&team=Y → .result-con (+.team, .result-score, .event-name)
"""
import re
import logging
from datetime import datetime
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from config import HLTV_BASE, TZ_NAME

log = logging.getLogger("hltv_api")

CF_TITLES = ("moment", "attention required", "cloudflare", "checking",
             "подождите", "момент")


# ---------------------------------------------------------------- helpers

async def wait_cloudflare(page, max_wait: int = 25) -> bool:
    """Ждём, пока Cloudflare пропустит страницу (V106: максимум 25 сек вместо 45)."""
    for _ in range(max_wait):
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        if title and not any(k in title for k in CF_TITLES):
            return True
        await page.wait_for_timeout(1000)
    return False


async def safe_content(page, marker: str | None = None, max_retries: int = 8) -> str:
    """Достаём HTML, при необходимости докручиваем ретраи (React/Cloudflare).
    V106: 8 попыток вместо 15 — цепочка не должна висеть минутами."""
    html = ""
    for i in range(max_retries):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            await page.wait_for_timeout(800)
            html = await page.content()
            if html and len(html) > 500 and (marker is None or marker in html):
                return html
        except Exception as e:
            log.debug("content retry %s/%s: %s", i + 1, max_retries, e)
        await page.wait_for_timeout(1500)
    return html


def _fmt_date_from_unix(ms: str | None) -> str:
    try:
        ts = int(ms) / 1000
        return datetime.fromtimestamp(ts, tz=ZoneInfo(TZ_NAME)).strftime("%d.%m.%y")
    except Exception:
        return ""


# ---------------------------------------------------------------- матчи

async def fetch_today_matches(page) -> list[dict]:
    """Список матчей со страницы /matches."""
    await page.goto(f"{HLTV_BASE}/matches", wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(2500)
    html = await safe_content(page, marker="match-wrapper")
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

        # время старта, если отрисовано
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

    log.info("Матчей найдено: %s", len(matches))
    return matches


# ---------------------------------------------------------------- карты

_MAP_RE = re.compile(r"^(.*?)\s*[-–—]?\s*(\d+(?:\.\d+)?)\s*%\s*$")
_MATCH_ID_RE = re.compile(r"/matches/(\d{4,})")   # V111: id страницы матча (scorebot)


async def fetch_team_maps(page, team_id: str, team_name: str) -> list[tuple[str, float]]:
    """Винрейты по картам со страницы stats. Возврат: [(карта, процент), ...]."""
    url = f"{HLTV_BASE}/stats/teams/maps/{team_id}/{quote(team_name)}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(2000)
    html = await safe_content(page, marker="map-pool-map-name")
    soup = BeautifulSoup(html, "html.parser")

    result: dict[str, float] = {}
    for div in soup.select(".map-pool-map-name"):
        text = div.get_text(strip=True)
        m = _MAP_RE.match(text)
        if m:
            map_name = m.group(1).strip(" -–—")
            result.setdefault(map_name, float(m.group(2)))

    # запасной путь: текст страницы (если класс переехал)
    if not result:
        for m in re.finditer(r"([A-Za-zА-Яа-я0-9 _\-]+?)\s*[-–—]\s*(\d+(?:\.\d+)?)%", html):
            result.setdefault(m.group(1).strip(), float(m.group(2)))

    maps = sorted(result.items(), key=lambda x: -x[1])
    log.info("Карты %s (%s): %s шт.", team_name, team_id, len(maps))
    return maps


async def fetch_team_form(page, team_id: str, team_name: str, n: int = 10) -> dict:
    """V115: форма команды (последние n матчей) со страницы результатов HLTV.

    Браузерный путь (Cloudflare не пускает обычный HTTP): /results?team=<id>.
    Возврат: {wins, total, streak, streak_won, last} — как у bo3data.
    """
    from hltv_http import parse_results_form  # общий парсер с HTTP-путём

    url = f"{HLTV_BASE}/results?team={team_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(1500)
    html = await safe_content(page, marker="result-con")
    form = parse_results_form(html, team_name, n)
    log.info("Форма %s (%s): %d/%d", team_name, team_id,
             form["wins"], form["total"])
    return form


# ---------------------------------------------------------------- рейтинги игроков (V113)

async def fetch_player_stats(page, team_id: str, team_name: str,
                             months: int = 3) -> list[dict]:
    """Рейтинги игроков команды с HLTV (rating 2.1 за последние N месяцев).

    Страница: /stats/players?team={id}&startDate=...&endDate=...
    Возврат: [{nickname, rating, maps, kd}, ...] — отсортировано по рейтингу.
    Пустой список = таблица не распарсилась (вызывающий просто не покажет блок).
    """
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=30 * months)
    url = (f"{HLTV_BASE}/stats/players?team={team_id}"
           f"&startDate={start.isoformat()}&endDate={end.isoformat()}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(2000)
    html = await safe_content(page, marker="stats-table")
    soup = BeautifulSoup(html, "html.parser")

    players: dict[str, dict] = {}
    table = soup.select_one("table.stats-table") or soup.find("table")
    rows = table.select("tbody tr") if table else []
    for tr in rows:
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if not cells:
            continue
        # ник: текст ссылки /stats/players/... или первая непустая ячейка
        nick = ""
        a = tr.find("a", href=re.compile(r"/stats/players/"))
        if a:
            nick = a.get_text(strip=True)
        if not nick:
            nick = cells[0]
        if not nick or nick.lower() in ("player",):
            continue
        # числа: rating = последняя ячейка вида 0.85–1.60;
        # K/D = похожая ячейка перед rating; maps = первая целая
        rating = kd = None
        try:
            nums = []
            for c in cells:
                try:
                    nums.append(float(c.replace("+", "").replace("−", "-")))
                except ValueError:
                    nums.append(None)
            for v in reversed(nums):
                if v is not None and 0.5 <= v <= 1.6:
                    rating = v
                    break
            for v in reversed(nums):
                if v is None or v == rating:
                    continue
                if 0.3 <= v <= 2.0:
                    kd = v
                    break
            maps = None
            for v in nums:
                if v is not None and 0 < v < 500 and float(v).is_integer():
                    maps = int(v)
                    break
        except Exception:
            rating = kd = maps = None
        rec = {"nickname": nick, "rating": rating, "kd": kd, "maps": maps}
        players.setdefault(nick, rec)

    out = sorted(players.values(),
                 key=lambda r: (r["rating"] or 0), reverse=True)
    log.info("Игроки %s (%s): %s шт. за %s мес.",
             team_name, team_id, len(out), months)
    return out


# ---------------------------------------------------------------- мировой ранг (V113)

async def fetch_team_rankings(page) -> dict:
    """Топ HLTV /ranking/teams/: {имя: {rank: int, points: str}}.

    Одна страница = весь топ-30. Имена берём как на HLTV — команды матчей
    тоже собраны с HLTV, поэтому совпадают почти всегда.
    """
    await page.goto(f"{HLTV_BASE}/ranking/teams/",
                    wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(2000)
    html = await safe_content(page, marker="ranked-team")
    soup = BeautifulSoup(html, "html.parser")

    out: dict[str, dict] = {}
    for team in soup.select(".ranked-team"):
        pos_el = team.select_one(".position")
        name_el = team.select_one(".name")
        pts_el = team.select_one(".points")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue
        rank = None
        if pos_el:
            m = re.search(r"\d+", pos_el.get_text(strip=True))
            if m:
                rank = int(m.group(0))
        points = pts_el.get_text(strip=True) if pts_el else ""
        out[name] = {"rank": rank, "points": points}

    log.info("Мировой ранг HLTV: %s команд", len(out))
    return out


# ---------------------------------------------------------------- H2H

async def fetch_h2h(page, id1: str, id2: str) -> list[dict]:
    """Личные встречи пары: /results?team=X&team=Y."""
    url = (f"{HLTV_BASE}/results?team={id1}&team={id2}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait_cloudflare(page)
    await page.wait_for_timeout(2000)
    html = await safe_content(page, marker="result-con")
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

    log.info("H2H %s vs %s: %s матчей", id1, id2, len(rows))
    return rows
