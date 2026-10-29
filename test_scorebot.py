"""
test_scorebot.py — проверка моста к HLTV scorebot БЕЗ Telegram.

Что делает:
  1) запускает Camoufox (тот же профиль hltv_profile);
  2) собирает список матчей HLTV (/matches) с id страниц;
  3) находит ИДУЩИЙ матч (или берёт id из аргумента);
  4) подписывается на scorebot и печатает всё, что прилетает, 90 секунд.

Запуск (в venv бота):
    python test_scorebot.py            # сам найдёт живой матч
    python test_scorebot.py 2381234    # конкретный HLTV-id матча

Что смотреть:
  • «scorebot: подписка на матч ...» — мост поднялся;
  • строки с RoundEnd/Kill/BombPlanted — события идут, всё работает;
  • «данных нет» — Cloudflare не пустил WS или матча нет в лайве;
  • «Неизвестное событие: ...» — новые типы событий, пришли в дамп.
"""
import asyncio
import sys
import time

import config
import database as db
import collector
import hltv_api
import scorebot


async def pick_live_match(explicit: str) -> tuple[str, str, str] | None:
    """(match_id, name1, name2) идущего матча HLTV."""
    if explicit:
        return explicit, f"Матч {explicit} — команда 1", "команда 2"
    async with collector.lock:
        browser = await collector.get_browser()
        page = await collector.new_page_clean(browser)
        try:
            matches = await hltv_api.fetch_today_matches(page)
        finally:
            try:
                await page.close()
            except Exception:
                pass
    if not matches:
        print("HLTV не отдал матчей — Cloudflare. Попробуй позже.")
        return None
    with_ids = [m for m in matches if m.get("match_id")]
    print(f"Матчей с id страницы: {len(with_ids)} из {len(matches)}")
    if not with_ids:
        return None
    # идущие обычно сверху страницы /matches; берём первый с id
    m = with_ids[0]
    return m["match_id"], m["team1_name"], m["team2_name"]


async def main():
    db.init_db()
    explicit = sys.argv[1] if len(sys.argv) > 1 else ""
    picked = await pick_live_match(explicit)
    if not picked:
        print("Не нашёл матч с id страницы. Запусти бота (кнопка Матчи сегодня), "
              "затем повтори тест, либо передай id вручную.")
        return
    mid, n1, n2 = picked
    print(f"Тестирую матч {mid}: {n1} vs {n2}")
    st = await scorebot.bridge.subscribe(mid, n1, n2)
    if st is None:
        print("Подписка не удалась (см. лог выше).")
        await scorebot.bridge.close()
        await collector.close_browser()
        return
    print("\n=== Слушаю scorebot 90 секунд (Ctrl+C — выйти) ===\n")
    t0 = time.time()
    last_render = 0.0
    while time.time() - t0 < 90:
        await scorebot.bridge.wait_update(mid, timeout=5)
        st = scorebot.bridge.state(mid)
        if st and st.got_data and time.time() - last_render > 10:
            last_render = time.time()
            s1, s2 = st.scores()
            a1, a2 = st.avgs()
            print(f"[{int(time.time()-t0):3d}s] счёт {s1}:{s2} · раунд {st.round_no} "
                  f"· карта {st.map_name or '?'} · деньги {st.money} "
                  f"· раунды: {list(st.last_rounds)[-3:]}")
        if st and st.match_over:
            print("Матч завершён по данным scorebot.")
            break
    st = scorebot.bridge.state(mid)
    if st:
        print("\n--- DEBUG-кольцо (неизвестные события, если есть) ---")
        for ts, key, raw in list(st.debug)[-20:]:
            print(f"• {key}: {raw[:180]}")
        if not st.debug:
            print("(пусто — парсер разобрал все события)")
    await scorebot.bridge.unsubscribe(mid)
    await scorebot.bridge.close()
    await collector.close_browser()
    print("\nГотово. Если счёт/раунды шли — мост работает, Лайв-центр в боте "
          "заработает с этим же кодом.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
