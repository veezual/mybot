"""
collector.py — сбор данных по требованию.

V106 (ЭПОХА bo3.gg + предохранители):
  • Матчи дня — ЦЕПОЧКА ИСТОЧНИКОВ, сбор не падает никогда:
      1) PandaScore (HTTP, ~3 сек) + добор пар из bo3.gg
      2) чистый bo3.gg (HTTP, ~1 сек, свои id 'b...')
      3) HLTV лёгкий HTTP (домашний IP обычно пускает, 1-3 сек)
      4) HLTV в Camoufox (запасной путь)
  • Карты/H2H/форма — bo3.gg (2-5 сек) -> HLTV HTTP -> Camoufox.
  • Camoufox — СИНГЛТОН: запускается один раз и живёт до остановки бота
    (раньше пересоздавался на каждый запрос — отсюда минуты ожидания);
    картинки/шрифты/медиа на страницах блокируются — загрузка быстрее.
  • kick_warmup()/kick_h2h_warmup() — фоновый прогрев карт, формы и H2H
    всех команд матчей дня (к клику данные уже лежат в базе).
  • odds_sweep()/ensure_odds() — кефы БК (Пари + Винлайн).

V108 (стабильность):
  • ВСЕ браузерные задачи (HLTV + браузерные БК) ходят через ОДИН синглтон —
    раньше сбор кефов запускал ВТОРОЙ Camoufox на том же профиле и падал
    с 'Failed to launch the browser process' (блокировка профиля);
  • умерший браузер корректно закрывается перед перезапуском (отпускает
    профиль), после неудачного запуска — минута тишины вместо шторма;

V111fix («всё виснет»):
  • запуск Camoufox ограничен таймаутом CAMOUFOX_LAUNCH_TIMEOUT (60 сек) —
    раньше Playwright ждал 180 сек, держа блокировку, и ВСЕ браузерные
    задачи (матчи, кефы, HLTV) стояли очередь по 3 минуты;
  • после неудачного запуска прибиваем зависшие процессы camoufox
    (taskkill/pkill) — раньше недозапущенный браузер оставался жив как
    зомби и БЛОКИРОВАЛ ПРОФИЛЬ, из-за чего все следующие запуски падали
    до перезапуска бота;
  • после 2 неудач подряд профиль переносится в hltv_profile_bad_<время>
    и создаётся чистый (куки HLTV теряются — зато браузер стартует);
  • пауза между попытками растёт: 1 мин -> 3 мин -> 10 мин -> 30 мин.
  • прогрев не долбит bo3.gg по командам, которые недавно не ответили;
  • фоновая HLTV-сверка не чаще раза в 5 минут.
"""
import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from config import PROFILE_DIR, HEADLESS, MATCHES_TTL, MAPS_TTL, H2H_TTL
import database as db
import sportsdata
import bo3data

log = logging.getLogger("collector")

# одновременно работаем только с одним браузером/страницами
lock = asyncio.Lock()


def _maps_to_dict(m) -> dict:
    """Любой формат карт -> словарь {карта: процент}.

    Фикс V106: HLTV иногда отдаёт записи из 3-4 полей (карта, %, победы,
    поражения) или пустые — раньше unpack 'k, v' падал с 'too many/not
    enough values to unpack'."""
    out: dict[str, float] = {}
    if isinstance(m, dict):
        for k, v in m.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(m, list):
        for item in m:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out[str(item[0]).strip()] = float(item[1])
            except (TypeError, ValueError):
                continue
    return out


# ------------------------------------------------------- V106: bo3.gg (быстро, без браузера)

_warm_lock = asyncio.Lock()

# V108: антишторм прогрева — команды/пары, по которым источник недавно не
# дал данных, не дёргаются повторно (раньше каждый клик заново гонял
# 10 неудачных запросов по тем же командам).
_warm_fail: dict[str, float] = {}   # ключ -> время последней неудачи
_warm_retry_at: dict[str, float] = {}   # V114: ключ -> время следующей попытки
_warm_last_run = 0.0                # время начала последнего полного прогрева


def _fail_wait(msg: str) -> float:
    """V114: «не найдена на bo3» / «нет карт в истории» — мёртвые или
    отсутствующие команды, само не вылечится — ждём сутки.
    Сетевые/HTTP ошибки — обычный короткий кулдаун."""
    m = str(msg)
    if ("не найдена" in m or "нет карт в истории" in m
            or "не удалось найти пару" in m):
        return config.WARMUP_FAIL_TTL_RESOLUTION
    return config.WARMUP_FAIL_COOLDOWN


async def _bo3_match_data(name1: str, name2: str,
                          need1: bool, need2: bool, need_h2h: bool):
    """Параллельный забор карт/формы/H2H через bo3.gg (HTTP).
    Возврат: (maps1, maps2, h2h|None, form1|None, form2|None, games1, games2).
    V109: games — {карта: число карт в выборке} для шринкейджа.
    Формы НЕ сохраняет — вызывающий сохранит под id из матча."""
    t1 = await bo3data.resolve_team(name1)
    t2 = await bo3data.resolve_team(name2)
    tasks, tags = [], []
    if need1 and t1:
        tasks.append(bo3data.team_stats(t1)); tags.append("m1")
    if need2 and t2:
        tasks.append(bo3data.team_stats(t2)); tags.append("m2")
    if need_h2h and t1 and t2:
        tasks.append(bo3data.h2h_rows(t1, t2)); tags.append("h")
    if not tasks:
        raise RuntimeError("bo3: нечего запрашивать/команды не найдены")
    res = await asyncio.gather(*tasks, return_exceptions=True)
    maps1 = maps2 = None
    h2h = form1 = form2 = None
    games1: dict = {}
    games2: dict = {}
    for tag, r in zip(tags, res):
        if isinstance(r, Exception):
            log.info("bo3 %s: %s", tag, r)
            continue
        if tag == "m1":
            # V109: team_stats -> (maps, form, games); защита от старого формата
            if isinstance(r, tuple) and len(r) == 3:
                maps1, form1, games1 = r
            elif isinstance(r, tuple) and len(r) == 2:
                maps1, form1 = r
            else:
                maps1 = r
        elif tag == "m2":
            if isinstance(r, tuple) and len(r) == 3:
                maps2, form2, games2 = r
            elif isinstance(r, tuple) and len(r) == 2:
                maps2, form2 = r
            else:
                maps2 = r
        else:
            h2h = r
    if maps1 is None and maps2 is None and h2h is None:
        raise RuntimeError("bo3: всё пусто")
    return maps1, maps2, h2h, form1, form2, games1, games2


# ---------------------------------------------------------------- прогрев

async def _acquire_warm_lock(timeout: float = 600.0) -> bool:
    """V115: прогрев ЖДЁТ освобождения лока (до timeout), а не молча сдавался.
    Раньше: сбор матчей запускал H2H-прогрев, тот садился на лок на пару минут,
    параллельный kick_warmup видел locked() и ТИХО уходил — команды оставались
    без статистики до следующего ручного пинка."""
    try:
        await asyncio.wait_for(_warm_lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _warm_fallback_hltv(team_id: str, team_name: str) -> bool:
    """V115: фолбэк прогрева — HLTV вместо bo3.gg.

    Для команд, которых bo3.gg не знает (Toobisie, NAOCOPULAS) или у которых
    там нет истории (свежие теги Arena Pro/Tactical Chaos/ex-INOX/BTH).
    Уровни: 1) HLTV HTTP (1-3 сек) -> 2) Camoufox-синглтон (штатный, как
    в ensure_maps). id может быть из bo3-namespace ('b…') — HLTV понимает
    только числовой, поэтому сначала пробуем обычные алиасы.
    Возврат: True, если хоть что-то сохранили.
    """
    cand = [str(team_id)]
    try:
        for a in db._alias_ids(str(team_id)):
            if a not in cand:
                cand.append(a)
    except Exception:
        pass
    # HLTV-страницы требуют числовой id: обычные — первыми, 'b…' — в конец
    cand = ([c for c in cand if not str(c).startswith("b")]
            + [c for c in cand if str(c).startswith("b")])[:3]

    from hltv_http import fetch_team_maps_http, fetch_team_form_http

    async def _try_http(tid: str) -> bool:
        got = False
        try:
            d = _maps_to_dict(await fetch_team_maps_http(tid, team_name))
            if d:
                db.replace_maps(team_id, d)
                got = True
        except Exception as e:
            log.info("фолбэк HLTV карты '%s' (%s): %s", team_name, tid, e)
        try:
            form = await fetch_team_form_http(tid, team_name)
            if form:
                db.replace_form(team_id, form)
                got = True
        except Exception as e:
            log.info("фолбэк HLTV форма '%s' (%s): %s", team_name, tid, e)
        return got

    for tid in cand:
        if await _try_http(tid):
            log.info("прогрев '%s': фолбэк HLTV HTTP сработал (id %s)", team_name, tid)
            return True

    # ---- уровень 2: Camoufox (Cloudflare не пустил обычный HTTP) ----
    try:
        async with lock:
            browser = await get_browser()
            page = await new_page_clean(browser)
            try:
                from hltv_api import fetch_team_maps as _bmaps, fetch_team_form as _bform
                for tid in cand:
                    got = False
                    try:
                        d = _maps_to_dict(await _bmaps(page, tid, team_name))
                        if d:
                            db.replace_maps(team_id, d)
                            got = True
                    except Exception as e:
                        log.info("фолбэк Camoufox карты '%s' (%s): %s", team_name, tid, e)
                    try:
                        form = await _bform(page, tid, team_name)
                        if form:
                            db.replace_form(team_id, form)
                            got = True
                    except Exception as e:
                        log.info("фолбэк Camoufox форма '%s' (%s): %s", team_name, tid, e)
                    if got:
                        log.info("прогрев '%s': фолбэк Camoufox сработал (id %s)",
                                 team_name, tid)
                        return True
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
    except Exception as e:
        log.info("фолбэк Camoufox '%s': %s", team_name, e)
    return False


def kick_warmup():
    """Фоновый прогрев карт+формы всех команд матчей дня (bo3.gg, секунды на команду).
    V115: свежесть проверяется по всем алиасам id; при пустом bo3 — HLTV-фолбэк."""
    async def _task():
        global _warm_last_run
        if not await _acquire_warm_lock():
            return
        try:
            # V108: полный прогрев не чаще, чем раз в WARMUP_MIN_INTERVAL
            if time.time() - _warm_last_run < config.WARMUP_MIN_INTERVAL:
                return
            _warm_last_run = time.time()
            matches = db.get_matches()
            jobs: list[tuple[str, str]] = []
            seen: set[str] = set()
            for m in matches:
                for tid, nm in ((m["team1_id"], m["team1_name"]),
                                (m["team2_id"], m["team2_name"])):
                    if not nm or nm in seen:
                        continue
                    seen.add(nm)
                    # V115: возраст по всем алиасам id — стата из другого
                    # namespace больше не считается отсутствующей
                    age_m = db.maps_age_any(tid)
                    age_f = db.form_age_any(tid)
                    need_m = age_m is None or age_m >= MAPS_TTL
                    need_f = age_f is None or age_f >= MAPS_TTL
                    if need_m or need_f:
                        jobs.append((str(tid), nm))
            # V108/V114: недавно неудачные команды не долбим повторно
            now_ts = time.time()
            jobs = [(tid, nm) for tid, nm in jobs
                    if now_ts >= _warm_retry_at.get(tid, 0.0)]
            if jobs:
                log.info("Прогрев карт+формы (bo3): %d команд", len(jobs))
            for tid, nm in jobs:
                try:
                    d, form, games = await bo3data.get_team_stats(nm)
                    db.replace_maps(tid, d, games)
                    if form:
                        db.replace_form(tid, form)
                    _warm_fail.pop(tid, None)
                    _warm_retry_at.pop(tid, None)
                    await asyncio.sleep(0.6)
                except Exception as e:
                    # V115: bo3 не дал данные — фолбэк на HLTV HTTP (карты+форма)
                    if await _warm_fallback_hltv(tid, nm):
                        _warm_fail.pop(tid, None)
                        _warm_retry_at.pop(tid, None)
                        continue
                    _warm_fail[tid] = time.time()
                    # V115: фолбэк мог не сработать разово (сеть/Cloudflare) —
                    # сутками не хороним, максимум 2 часа
                    wait = min(_fail_wait(e), 2 * 3600)
                    _warm_retry_at[tid] = time.time() + wait
                    log.info("прогрев %s: %s (повтор через %.0f мин)",
                             nm, e, wait / 60)
        except Exception as e:
            log.warning("прогрев: %s", e)
        finally:
            _warm_lock.release()
    return asyncio.create_task(_task())


def kick_h2h_warmup():
    """Фоновый прогрев H2H всех пар матчей дня (bo3.gg, ~0.5 сек на пару).
    V115: ждёт лок вместо тихого выхода; свежесть — по алиасам пары;
    если bo3 не дал H2H — фолбэк на HLTV HTTP."""
    async def _task():
        if not await _acquire_warm_lock():
            return
        try:
            matches = db.get_matches()
            pairs, seen = [], set()
            for m in matches:
                id1, id2 = str(m["team1_id"]), str(m["team2_id"])
                key = tuple(sorted((id1, id2)))
                if key in seen:
                    continue
                seen.add(key)
                # V115: алиас-свежесть — пара из другого namespace не пересобирается
                age = db.h2h_age_any(id1, id2)
                if (db.get_h2h_any(id1, id2) and age is not None
                        and age < H2H_TTL):
                    continue
                pairs.append((id1, id2, m["team1_name"], m["team2_name"]))
            # V108/V114: пары с недавней неудачей пропускаем
            now_ts = time.time()
            pairs = [(id1, id2, n1, n2) for id1, id2, n1, n2 in pairs
                     if now_ts >= _warm_retry_at.get(f"h|{id1}|{id2}", 0.0)]
            if pairs:
                log.info("Прогрев H2H (bo3): %d пар", len(pairs))
            for id1, id2, n1, n2 in pairs:
                pk = f"h|{id1}|{id2}"
                try:
                    rows = await bo3data.get_h2h(n1, n2)
                    if rows:
                        db.replace_h2h(id1, id2, rows)
                        _warm_fail.pop(pk, None)
                        _warm_retry_at.pop(pk, None)
                    else:
                        # пустой H2H — легитимно (команды не встречались);
                        # долго не переспрашиваем
                        _warm_fail[pk] = time.time()
                        _warm_retry_at[pk] = (time.time()
                                              + config.WARMUP_FAIL_TTL_RESOLUTION)
                except Exception as e:
                    # V115: bo3 не дал H2H — фолбэк на HLTV HTTP (без браузера)
                    got = False
                    try:
                        from hltv_http import fetch_h2h_http
                        rows = await fetch_h2h_http(id1, id2)
                        if rows:
                            db.replace_h2h(id1, id2, rows)
                            got = True
                    except Exception as e2:
                        log.info("h2h фолбэк HLTV %s vs %s: %s", n1, n2, e2)
                    if got:
                        _warm_fail.pop(pk, None)
                        _warm_retry_at.pop(pk, None)
                    else:
                        _warm_fail[pk] = time.time()
                        # V115: сутками не хороним — фолбэк мог споткнуться разово
                        wait = min(_fail_wait(e), 2 * 3600)
                        _warm_retry_at[pk] = time.time() + wait
                        log.info("прогрев h2h %s: %s", n1, e)
                await asyncio.sleep(0.4)
        except Exception as e:
            log.warning("прогрев h2h: %s", e)
        finally:
            _warm_lock.release()
    return asyncio.create_task(_task())


def _import_camoufox():
    try:
        from camoufox.async_api import AsyncCamoufox
        return AsyncCamoufox
    except ImportError as e:
        raise RuntimeError(
            "Camoufox не установлен. Запусти install_camoufox.bat "
            "(pip install camoufox[geoip] && python -m camoufox fetch)"
        ) from e


def _camoufox_kwargs():
    kw = dict(
        persistent_context=True,
        user_data_dir=PROFILE_DIR,
        headless=HEADLESS,
        humanize=True,
    )
    # GeoIP улучшает подмену локали, но требует mmdb-базы (качается при `camoufox fetch`).
    # Если базы нет (например, GitHub отдал 403) — запускаемся без неё, чтобы не падать.
    try:
        from pathlib import Path
        from camoufox.geolocation import get_mmdb_path
        kw["geoip"] = bool(get_mmdb_path().exists())
    except Exception:
        pass
    return kw


# --------------------------------------- V106: браузер-синглтон

_browser = None          # живой BrowserContext Camoufox
_browser_cm = None       # его context manager (для корректного закрытия)

# V108: защита от «шторма перезапусков». Симптом из логов: Camoufox умер,
# и все задачи разом кидались перезапускать его — процесс вылетал с
# exitCode=0 (профиль занят зомби или вторым экземпляром).
_launch_lock = asyncio.Lock()      # перезапуск строго по одному
_launch_last_fail = 0.0            # время последней неудачной попытки
_LAUNCH_RETRY_WAIT = 60.0          # сек тишины после первой неудачи
_launch_fail_streak = 0            # V111fix: подряд неудачных запусков
_LAUNCH_BACKOFF_STEPS = (60.0, 180.0, 600.0, 1800.0)


def _launch_retry_wait() -> float:
    """V111fix: пауза растёт с числом неудач подряд (не долбим по кругу)."""
    idx = max(_launch_fail_streak - 1, 0)
    return _LAUNCH_BACKOFF_STEPS[min(idx, len(_LAUNCH_BACKOFF_STEPS) - 1)]


def _kill_zombie_camoufox():
    """V111fix: прибить недозапущенные процессы camoufox.
    Вызывается ТОЛЬКО после неудачного запуска: живой синглтон в этот
    момент не работает (мы под _launch_lock), так что риску нет.
    Без этого зомби держал бы профиль, и все следующие запуски падали
    до перезапуска бота."""
    for cmd in (["taskkill", "/F", "/IM", "camoufox.exe", "/T"],   # Windows
                ["pkill", "-f", "camoufox"]):                      # Linux/macOS
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
            return
        except FileNotFoundError:
            continue        # не та платформа — пробуем следующую команду
        except Exception:
            return


def _reset_profile_dir():
    """V111fix: профиль мог остаться заблокированным зомби-процессом.
    После 2 неудач подряд переносим его в сторону — следующий запуск
    создаст чистый (куки Cloudflare/HLTV теряются, зато запускается)."""
    try:
        p = Path(PROFILE_DIR)
        if p.exists():
            bad = p.parent / f"hltv_profile_bad_{int(time.time())}"
            os.rename(p, bad)
            log.warning("Профиль Camoufox не дал запуститься дважды — перенос "
                        "в %s, стартую с чистым профилем", bad.name)
    except Exception as e:
        log.warning("Не удалось перенести профиль Camoufox: %s", e)


async def _kill_browser():
    """Добить старый (возможно зомби) экземпляр, отпустив профиль."""
    global _browser, _browser_cm
    if _browser_cm is not None:
        try:
            await asyncio.wait_for(_browser_cm.__aexit__(None, None, None),
                                   timeout=15)
        except Exception:
            pass
    _browser = None
    _browser_cm = None


async def get_browser():
    """Ленивый синглтон Camoufox. ВЫЗЫВАТЬ ПОД collector.lock!
    Живёт до остановки бота — профиль/куки Cloudflare сохраняются,
    повторные запросы не платят 10-20 сек за запуск Firefox.
    V108: если браузер умер — старый экземпляр корректно закрывается
    (отпускает профиль) и только потом стартует новый.
    V111fix: запуск ограничен таймаутом (по умолчанию 60 сек вместо
    180 у Playwright); после неудачи прибиваем зомби-процессы camoufox,
    после 2 неудач переносим заблокированный профиль, пауза между
    попытками растёт (1/3/10/30 мин)."""
    global _browser, _browser_cm, _launch_last_fail, _launch_fail_streak
    async with _launch_lock:
        if _browser is not None:
            try:
                # V112fix: в playwright (async API) у BrowserContext `pages` —
                # СВОЙСТВО (список), а не метод. Раньше `_browser.pages()`
                # падало с TypeError ('list' object is not callable), браузер
                # считался мёртвым, УБИВАЛСЯ и перезапускался при каждом
                # обращении — вместе с ним умирала страница-мост scorebot
                # и все открытые вкладки (в логе: «Camoufox запущен» 2+ раза
                # за минуту). Теперь работаем и со свойством, и с методом.
                pages = getattr(_browser, "pages")
                if callable(pages):
                    pages = pages()
                if asyncio.iscoroutine(pages):
                    pages = await asyncio.wait_for(pages, timeout=8)
                list(pages)                     # дешёвая проверка живости
                return _browser
            except Exception:
                await _kill_browser()           # отпускаем профиль
        wait = _launch_retry_wait()
        if time.time() - _launch_last_fail < wait:
            left = int(wait - (time.time() - _launch_last_fail))
            raise RuntimeError(
                f"Camoufox недавно не смог запуститься — повторная попытка "
                f"через ~{left // 60 + 1} мин (данные обычно приходят "
                f"из bo3.gg/PandaScore)")
        AsyncCamoufox = _import_camoufox()
        _browser_cm = AsyncCamoufox(**_camoufox_kwargs())
        try:
            _browser = await asyncio.wait_for(
                _browser_cm.__aenter__(),
                timeout=config.CAMOUFOX_LAUNCH_TIMEOUT)
        except Exception as e:
            _launch_last_fail = time.time()
            _launch_fail_streak += 1
            _browser_cm = None                  # cm недозапущен — не трогаем
            _kill_zombie_camoufox()             # прибить процесс, отпустить профиль
            if _launch_fail_streak >= 2:
                _reset_profile_dir()            # заблокированный профиль — в сторону
            log.warning("Camoufox не запустился (%s) — пауза %.0f сек",
                        str(e)[:200], _launch_retry_wait())
            raise
        _launch_last_fail = 0.0
        _launch_fail_streak = 0
        log.info("Camoufox запущен (синглтон, живёт до остановки бота)")
        return _browser


async def close_browser():
    """Корректное закрытие при остановке бота."""
    global _browser, _browser_cm
    if _browser_cm is not None:
        try:
            await _browser_cm.__aexit__(None, None, None)
        except Exception:
            pass
    _browser = None
    _browser_cm = None


# блокируем тяжёлые ресурсы — страницы грузятся в разы быстрее.
# JS НЕ трогаем (нужен для проверки Cloudflare), CSS тоже оставляем.
_BLOCK_TYPES = ("image", "media", "font")


async def new_page_clean(browser):
    """Новая страница с блокировкой картинок/шрифтов/медиа."""
    page = await browser.new_page()

    async def _block(route):
        try:
            if route.request.resource_type in _BLOCK_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    try:
        await page.route("**/*", _block)
    except Exception:
        pass
    return page


# ------------------------------------------------------- матчи дня (БЫСТРО)

async def collect_matches_only(progress_cb=None, force: bool = False) -> list[dict]:
    """
    HLTV-сбор матчей: СНАЧАЛА лёгкий HTTP (1-3 сек, домашний IP обычно
    пускает), только если Cloudflare преградил — Camoufox (синглтон).
    Карты команд и H2H НЕ трогаем — они собираются лениво при клике.
    """
    age = db.matches_age()
    if not force and age is not None and age < MATCHES_TTL:
        cached = db.get_matches()
        if cached:
            return cached

    # ---- Шаг 1: лёгкий HTTP без браузера (V106) ----
    try:
        from hltv_http import fetch_today_matches_http
        matches = await fetch_today_matches_http()
        if matches:
            db.replace_matches(matches)
            db.replace_hltv_matches(matches)   # V111: id страниц для scorebot
            for m in matches:
                db.upsert_team(m["team1_id"], m["team1_name"], m.get("team1_logo"))
                db.upsert_team(m["team2_id"], m["team2_name"], m.get("team2_logo"))
            db.set_meta("last_collect", str(time.time()))
            db.set_meta("last_matches_source", "hltv_http")
            return db.get_matches()
    except Exception as e:
        log.info("HLTV HTTP: %s — пробую браузер", e)

    # ---- Шаг 2: Camoufox (синглтон, страницы с блокировкой мусора) ----
    async with lock:
        if progress_cb:
            await progress_cb("matches", 0, 0, "")
        browser = await get_browser()
        page = await new_page_clean(browser)
        try:
            from hltv_api import fetch_today_matches
            matches = await fetch_today_matches(page)
        finally:
            try:
                await page.close()
            except Exception:
                pass
        if not matches:
            raise RuntimeError(
                "HLTV не отдал матчей (Cloudflare). Но матчи могли прийти "
                "из bo3.gg/PandaScore — нажми «Матчи сегодня» ещё раз.")

        db.replace_matches(matches)
        db.replace_hltv_matches(matches)   # V111: id страниц для scorebot
        for m in matches:
            db.upsert_team(m["team1_id"], m["team1_name"], m.get("team1_logo"))
            db.upsert_team(m["team2_id"], m["team2_name"], m.get("team2_logo"))
        db.set_meta("last_collect", str(time.time()))
        db.set_meta("last_matches_source", "hltv_browser")

    return db.get_matches()


async def collect_all(progress_cb=None, force: bool = False) -> list[dict]:
    """Совместимость со старыми вызовами: теперь это быстрый сбор матчей."""
    return await collect_matches_only(progress_cb=progress_cb, force=force)


# --------------------------------------- V106: чистый bo3.gg список матчей

def _upsert_teams_nodup(teams: list[tuple[str, str, str]]):
    """upsert_team, но НЕ создаёт дубликатов по имени (HLTV-id уже есть)."""
    from odds_api import norm_name
    existing = {}
    for t in db.get_all_teams():
        existing.setdefault(norm_name(t["name"]), t["hltv_id"])
    for tid, name, logo in teams:
        if norm_name(name) in existing:
            continue
        db.upsert_team(tid, name, logo or None)
        existing.setdefault(norm_name(name), tid)


async def _bo3_matches_rows() -> list[dict]:
    """Матчи дня из bo3.gg (без записи в базу). id команд: 'b<bo3_id>'."""
    now_msk = datetime.now(ZoneInfo(config.TZ_NAME))
    date_iso = now_msk.strftime("%Y-%m-%d")
    rows_bo3, _teams = await bo3data.upcoming_matches(date_iso)
    rows = []
    for m in rows_bo3:
        start = ""
        try:
            dt = datetime.fromisoformat(m["start"])
            start = (dt.replace(tzinfo=ZoneInfo("UTC"))
                     .astimezone(ZoneInfo(config.TZ_NAME)).strftime("%H:%M"))
        except Exception:
            pass
        rows.append({
            "team1_id": "b" + str(m["id1"]), "team1_name": m["n1"],
            "team2_id": "b" + str(m["id2"]), "team2_name": m["n2"],
            "start_time": start,
            "tournament": m.get("tier") or "",
        })
    return rows


async def collect_matches_bo3() -> list[dict]:
    """Второй быстрый источник списка матчей: чистый bo3.gg (HTTP, ~1 сек).
    Работает даже на ПУСТОЙ базе — мэтчинг с HLTV не нужен.
    Команды регистрируются в базе со своими id ('b...'), прогноз и
    карты/H2H работают по именам через bo3.gg."""
    rows = await _bo3_matches_rows()
    if not rows:
        raise RuntimeError("bo3.gg не отдал матчей на сегодня")
    # регистрируем команды (без дублей по имени)
    from odds_api import norm_name
    teams: list[tuple[str, str, str]] = []
    for m in rows:
        teams.append((m["team1_id"], m["team1_name"], ""))
        teams.append((m["team2_id"], m["team2_name"], ""))
    _upsert_teams_nodup(teams)
    db.replace_matches(rows)
    db.set_meta("last_collect", str(time.time()))
    db.set_meta("last_matches_source", "bo3")
    log.info("Матчи из bo3.gg: %d шт.", len(rows))
    return db.get_matches()


async def collect_matches_via_sources() -> tuple[list[dict], int] | None:
    """
    Быстрый путь (V106): матчи из внешних источников (PandaScore) + мэтчинг
    с HLTV-ID из базы + ДОБОР ПАР из bo3.gg (у которых нет HLTV-сопоставления).
    Без браузера, секунды.

    Возврат: (матчи для таблицы matches, сколько пар не сопоставилось с HLTV)
             либо None, если PandaScore недоступен.
    """
    try:
        unified = await sportsdata.get_upcoming()
    except Exception as e:
        log.warning("Быстрый путь (источники): %s", e)
        return None
    if not unified:
        return None

    teams_rows = db.get_all_teams()
    if len(teams_rows) < 5:
        # база почти пуста — мэтчить не к кому; чистый bo3.gg справится лучше
        return None

    try:
        rows, unresolved = await sportsdata.resolve_matches_to_hltv(unified)
    except Exception as e:
        log.warning("Мэтчинг команд: %s", e)
        return None
    if len(rows) < 3:
        log.info("Быстрый путь: сопоставилось только %d матчей — используется bo3/HLTV", len(rows))
        return None

    # обогащение логотипами (только если у команды в базе логотипа нет)
    for r in rows:
        for tid, tlogo in ((r["team1_id"], r["team1_logo"]),
                           (r["team2_id"], r["team2_logo"])):
            if tlogo:
                trow = db.get_team(tid)
                if trow and not trow["logo_url"]:
                    db.upsert_team(tid, trow["name"], tlogo)
        r.pop("team1_logo", None)
        r.pop("team2_logo", None)

    # ---- V106: добираем пары из bo3.gg (id 'b...'), которых нет в списке ----
    merged = 0
    try:
        bo3_rows = await _bo3_matches_rows()
        from odds_api import norm_name as _nn
        have = set()
        for r in rows:
            have.add(tuple(sorted((_nn(r["team1_name"]), _nn(r["team2_name"])))))
        for m in bo3_rows:
            key = tuple(sorted((_nn(m["team1_name"]), _nn(m["team2_name"]))))
            if key not in have:
                rows.append(m)
                have.add(key)
                merged += 1
        if merged:
            _upsert_teams_nodup(
                [(m["team1_id"], m["team1_name"], "") for m in bo3_rows]
                + [(m["team2_id"], m["team2_name"], "") for m in bo3_rows])
            log.info("bo3-добор: +%d пар без HLTV-сопоставления", merged)
    except Exception as e:
        log.info("bo3-добор: %s", e)

    db.replace_matches(rows)
    db.set_meta("last_collect", str(time.time()))
    db.set_meta("last_matches_source", "pandascore+bo3")
    log.info("Матчи из источников: %d шт. (без HLTV-сопоставления: %d)",
             len(rows), unresolved + merged)
    return db.get_matches(), unresolved + merged


# ------------------------------------------------------- данные одного матча

async def ensure_match_data(id1: str, name1: str, id2: str, name2: str,
                            progress_cb=None) -> tuple[dict, dict, list]:
    """
    Всё нужное для прогноза ОДНОГО матча:
      1) карты команды 1 и команды 2 — ПАРАЛЛЕЛЬНО (2 страницы одного браузера)
      2) личные встречи пары (если кэш свежий — пропускаем)
    Кэш перепроверяется ПОСЛЕ входа в блокировку: если ты кликнул дважды,
    второй клик дождётся очереди и увидит свежий кэш — пересбора не будет.
    Возврат: (maps1: dict, maps2: dict, h2h_rows: list).
    """
    maps1 = db.get_maps(id1)[0]
    maps2 = db.get_maps(id2)[0]
    h2h_rows = db.get_h2h(id1, id2)
    need1 = not maps1 or (db.maps_age(id1) or 1e9) >= MAPS_TTL
    need2 = not maps2 or (db.maps_age(id2) or 1e9) >= MAPS_TTL
    need_h2h = not h2h_rows or (db.h2h_age(id1, id2) or 1e9) >= H2H_TTL
    # V107: форма — самостоятельный компонент модели (вес 0.30). Раньше при
    # свежих картах форма не запрашивалась вовсе, и модель молча считала
    # прогноз без неё (кейс FUT vs Nemiga: 80% уверенности без формы).
    need_f1 = db.get_form(id1) is None or (db.form_age(id1) or 1e9) >= MAPS_TTL
    need_f2 = db.get_form(id2) is None or (db.form_age(id2) or 1e9) >= MAPS_TTL

    if not (need1 or need2 or need_h2h or need_f1 or need_f2):
        return maps1, maps2, h2h_rows  # всё свежее — браузер не нужен вообще

    # ---- V106: БЫСТРЫЙ путь bo3.gg (HTTP, 2-5 сек, без браузера) + форма ----
    try:
        b1, b2, bh, f1, f2, g1, g2 = await _bo3_match_data(
            name1, name2, need1 or need_f1, need2 or need_f2, need_h2h)
        if b1 and (need1 or need_f1):
            db.replace_maps(id1, b1, g1)
            maps1 = b1  # только что собранное — свежее кэша
        if b2 and (need2 or need_f2):
            db.replace_maps(id2, b2, g2)
            maps2 = b2
        if need_h2h and bh is not None:
            db.replace_h2h(id1, id2, bh)
            h2h_rows = bh
        if f1:
            db.replace_form(id1, f1)
        if f2:
            db.replace_form(id2, f2)
        if (not need1 or maps1) and (not need2 or maps2) and (not need_h2h or h2h_rows):
            return maps1, maps2, h2h_rows
        # что-то одно не вышло — недостающее доберёт HLTV HTTP/Camoufox ниже
    except Exception as e:
        log.info("bo3 быстрый путь (%s vs %s): %s — пробую HLTV HTTP", name1, name2, e)

    # ---- V106: лёгкий HTTP до HLTV (без браузера, 1-3 сек) ----
    if (need1 and not maps1) or (need2 and not maps2) or (need_h2h and not h2h_rows):
        try:
            from hltv_http import fetch_team_maps_http, fetch_h2h_http
            if need1 and not maps1:
                mm = await fetch_team_maps_http(id1, name1)
                d = _maps_to_dict(mm)
                if d:
                    db.replace_maps(id1, d)
                    maps1 = d
            if need2 and not maps2:
                mm = await fetch_team_maps_http(id2, name2)
                d = _maps_to_dict(mm)
                if d:
                    db.replace_maps(id2, d)
                    maps2 = d
            if need_h2h and not h2h_rows:
                hr = await fetch_h2h_http(id1, id2)
                if hr:
                    db.replace_h2h(id1, id2, hr)
                    h2h_rows = db.get_h2h(id1, id2)
            if (not need1 or maps1) and (not need2 or maps2) and (not need_h2h or h2h_rows):
                return maps1, maps2, h2h_rows
        except Exception as e:
            log.info("HLTV HTTP добор (%s vs %s): %s — Camoufox", name1, name2, e)

    # ---- V115: форма с HLTV HTTP, если bo3 её не дал (свежие теги и т.п.) ----
    if (need_f1 and db.get_form_any(id1) is None) or \
       (need_f2 and db.get_form_any(id2) is None):
        try:
            from hltv_http import fetch_team_form_http
            if need_f1 and db.get_form_any(id1) is None:
                try:
                    db.replace_form(id1, await fetch_team_form_http(id1, name1))
                except Exception as e:
                    log.info("форма HLTV '%s': %s", name1, e)
            if need_f2 and db.get_form_any(id2) is None:
                try:
                    db.replace_form(id2, await fetch_team_form_http(id2, name2))
                except Exception as e:
                    log.info("форма HLTV '%s': %s", name2, e)
        except ImportError as e:
            log.info("фолбэк формы недоступен: %s", e)

    async with lock:
        # ---- ПЕРЕПРОВЕРКА кэша после очереди (фикс двойного сбора V105) ----
        maps1 = db.get_maps(id1)[0]
        maps2 = db.get_maps(id2)[0]
        h2h_rows = db.get_h2h(id1, id2)
        need1 = not maps1 or (db.maps_age(id1) or 1e9) >= MAPS_TTL
        need2 = not maps2 or (db.maps_age(id2) or 1e9) >= MAPS_TTL
        need_h2h = not h2h_rows or (db.h2h_age(id1, id2) or 1e9) >= H2H_TTL
        if not (need1 or need2 or need_h2h):
            return maps1, maps2, h2h_rows

        # ---- V106: Camoufox-СИНГЛТОН (запускается один раз, живёт до стопа) ----
        browser = await get_browser()
        from hltv_api import fetch_team_maps, fetch_h2h

        async def _collect_maps(page, team_id: str, team_name: str) -> dict:
            """Карты команды -> ВСЕГДА словарь (фикс unpack-багов V106)."""
            try:
                m = await fetch_team_maps(page, team_id, team_name)
                if m:
                    d = _maps_to_dict(m)
                    if d:
                        db.replace_maps(team_id, d)
                        return d
            except Exception as e:
                log.warning("Карты %s: %s", team_name, e)
            return {}

        # ---- карты: параллельно, каждая команда на своей странице ----
        if need1 or need2:
            if progress_cb:
                teams_txt = " + ".join(x for x, need in ((name1, need1), (name2, need2)) if need)
                await progress_cb("maps", 1, 3, teams_txt)
            pages = [await new_page_clean(browser) for _ in range(2 if (need1 and need2) else 1)]
            try:
                tasks = []
                if need1:
                    tasks.append(_collect_maps(pages[0], id1, name1))
                if need2:
                    tasks.append(_collect_maps(pages[1 if need1 else 0], id2, name2))
                got = await asyncio.gather(*tasks, return_exceptions=True)
                got_maps = [g for g in got if isinstance(g, dict)]
                if need1 and need2 and len(got_maps) == 2:
                    maps1, maps2 = got_maps[0], got_maps[1]
                elif got_maps:
                    # порядок gather соответствует порядку tasks
                    vals = list(got)
                    if need1:
                        v = vals[0]
                        if isinstance(v, dict):
                            maps1 = v
                    if need2:
                        v = vals[1] if (need1 and len(vals) > 1) else vals[0]
                        if isinstance(v, dict):
                            maps2 = v
            finally:
                for p in pages:
                    try:
                        await p.close()
                    except Exception:
                        pass

        # ---- H2H после карт (та же сессия браузера) ----
        if need_h2h:
            if progress_cb:
                await progress_cb("h2h", 3, 3, name2)
            page = await new_page_clean(browser)
            try:
                rows = await fetch_h2h(page, id1, id2)
                db.replace_h2h(id1, id2, rows)
                h2h_rows = db.get_h2h(id1, id2)
            except Exception as e:
                log.warning("H2H %s vs %s: %s", name1, name2, e)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    # страховка: наружу уходят только словари
    if not isinstance(maps1, dict):
        maps1 = {k: v for k, v in (maps1 or [])} if maps1 else {}
    if not isinstance(maps2, dict):
        maps2 = {k: v for k, v in (maps2 or [])} if maps2 else {}
    return maps1, maps2, h2h_rows


# ------------------------------------------------------- карты одной команды

async def ensure_maps(team_id: str, team_name: str) -> dict[str, float]:
    stale = db.maps_age(team_id)
    if stale is None or stale >= MAPS_TTL:
        # V106: сначала bo3.gg (секунды, + форма впридачу)
        try:
            d, form = await bo3data.get_team_stats(team_name)
            db.replace_maps(team_id, d)
            if form:
                db.replace_form(team_id, form)
        except Exception as e:
            log.info("bo3 карты '%s': %s — HLTV HTTP", team_name, e)
            # V106: лёгкий HTTP до HLTV (без браузера)
            got = False
            try:
                from hltv_http import fetch_team_maps_http
                mm = await fetch_team_maps_http(team_id, team_name)
                d = _maps_to_dict(mm)
                if d:
                    db.replace_maps(team_id, d)
                    got = True
            except Exception as e2:
                log.info("HLTV HTTP карты '%s': %s — Camoufox", team_name, e2)
            if not got:
                async with lock:
                    browser = await get_browser()
                    page = await new_page_clean(browser)
                    from hltv_api import fetch_team_maps
                    try:
                        maps = await fetch_team_maps(page, team_id, team_name)
                        if maps:
                            d = _maps_to_dict(maps)  # фикс unpack-багов
                            if d:
                                db.replace_maps(team_id, d)
                    except Exception as e3:
                        log.warning("Карты %s: %s", team_name, e3)
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass
    result, _ = db.get_maps(team_id)
    return result


# ------------------------------------------------------- H2H

async def ensure_h2h(id1: str, id2: str, name1: str = "", name2: str = "") -> list[dict]:
    stale = db.h2h_age(id1, id2)
    if stale is None or stale >= H2H_TTL:
        # V106: сначала bo3.gg (нужны имена; для старых вызовов — достанем из базы)
        if not name1 or not name2:
            t1, t2 = db.get_team(id1), db.get_team(id2)
            name1 = name1 or (t1["name"] if t1 else "")
            name2 = name2 or (t2["name"] if t2 else "")
        if name1 and name2:
            try:
                rows = await bo3data.get_h2h(name1, name2)
                db.replace_h2h(id1, id2, rows)
                return db.get_h2h(id1, id2)
            except Exception as e:
                log.info("bo3 h2h %s vs %s: %s — HLTV HTTP", name1, name2, e)
        # V106: лёгкий HTTP до HLTV (без браузера)
        got = False
        try:
            from hltv_http import fetch_h2h_http
            rows = await fetch_h2h_http(id1, id2)
            if rows:
                db.replace_h2h(id1, id2, rows)
                got = True
        except Exception as e:
            log.info("HLTV HTTP h2h %s vs %s: %s — Camoufox", name1, name2, e)
        if not got:
            async with lock:
                browser = await get_browser()
                page = await new_page_clean(browser)
                from hltv_api import fetch_h2h
                try:
                    rows = await fetch_h2h(page, id1, id2)
                    db.replace_h2h(id1, id2, rows)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
    return db.get_h2h(id1, id2)


# ------------------------------------------------------- кефы БК (Этапы 2-3)

async def _fetch_http_bookmakers(collected: dict, status_cb=None):
    """Пари и Фонбет — прямой HTTP, браузер не нужен."""
    import odds_api
    from config import ODDS_ENABLED
    for bk, fn in (("pari", odds_api.fetch_pari), ("fonbet", odds_api.fetch_fonbet)):
        if bk not in ODDS_ENABLED:
            continue
        if status_cb:
            await status_cb(f"Снимаю линию {bk}…")
        try:
            pairs = await fn()
            for p in pairs:
                na, nb = odds_api.norm_name(p["team1"]), odds_api.norm_name(p["team2"])
                if na and nb and na != nb:
                    collected.setdefault(tuple(sorted((na, nb))), {})[bk] = p
        except Exception as e:
            log.warning("kefy %s: %s", bk, e)


async def _fetch_browser_bookmakers(collected: dict, status_cb=None):
    """Винлайн/Бетбум — страница линии в Camoufox.
    V108: через ОБЩИЙ синглтон. Раньше здесь запускался ВТОРОЙ браузер на
    том же профиле — при живом синглтоне он гарантированно падал с
    'Failed to launch the browser process' (блокировка профиля)."""
    import odds_api
    from config import ODDS_ENABLED
    if not any(bk in ODDS_ENABLED for bk in ("winline", "betboom")):
        return
    async with lock:
        browser = await get_browser()
        page = await new_page_clean(browser)
        try:
            for bk in ("winline", "betboom"):
                if bk not in ODDS_ENABLED:
                    continue
                if status_cb:
                    await status_cb(f"Захожу на линию {bk}…")
                try:
                    if bk == "winline":
                        pairs = await odds_api.fetch_winline(page)
                    else:
                        pairs = await odds_api.fetch_betboom(page)
                    for p in pairs:
                        na, nb = odds_api.norm_name(p["team1"]), odds_api.norm_name(p["team2"])
                        if na and nb and na != nb:
                            collected.setdefault(tuple(sorted((na, nb))), {})[bk] = p
                except Exception as e:
                    log.warning("kefy %s: %s", bk, e)
        finally:
            try:
                await page.close()
            except Exception:
                pass


async def odds_sweep(status_cb=None, with_browser: bool = True) -> int:
    """
    Общий сбор кефов со всех включённых БК в базу (для value-скана).
    Возврат: сколько пар сохранено.
    """
    import odds_api
    collected: dict = {}
    await _fetch_http_bookmakers(collected, status_cb)
    if with_browser:
        try:
            await _fetch_browser_bookmakers(collected, status_cb)
        except Exception as e:
            # V108: браузерные БК не должны хоронить результаты HTTP-БК (Пари)
            log.warning("kefy browser: %s", e)

    teams = db.get_all_teams()
    id_lookup = {}
    for t in teams:
        id_lookup[odds_api.norm_name(t["name"])] = t["hltv_id"]
        toks = odds_api.norm_name(t["name"]).split()
        if toks:
            id_lookup.setdefault(toks[-1], t["hltv_id"])
    saved = db.save_odds_sweep(collected, id_lookup)
    db.set_meta("last_odds_sweep", str(time.time()))
    return saved


async def ensure_odds(id1: str, id2: str, name1: str, name2: str,
                      status_cb=None) -> dict[str, tuple[float, float]]:
    """
    Кефы пары {bookmaker: (w1, w2)}.
    Если кэш устарел — общий сбор со всех БК (заполняет базу и для скана).
    """
    key = ":".join(sorted([str(id1), str(id2)]))
    from config import ODDS_TTL
    cached = db.get_odds(key, ODDS_TTL)
    if cached:
        return cached

    await odds_sweep(status_cb=status_cb, with_browser=True)

    result = db.get_odds(key, ODDS_TTL)
    if not result and status_cb:
        await status_cb("Кефов на эту пару в линиях БК не нашлось")
    return result
