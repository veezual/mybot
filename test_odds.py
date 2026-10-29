"""
test_odds.py — проверка сбора кефов БК прямо на твоей машине (RU IP).

Запуск:
    venv\\Scripts\\activate.bat
    python test_odds.py

Скрипт опрашивает все включённые БК и печатает найденные пары с кефами.
Всё, что не удалось разобрать, сохраняется в debug_odds/*.json —
скинь эти файлы разработчику, и парсеры докручиваются за пару минут.
"""
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> int:
    from config import ODDS_ENABLED, PROFILE_DIR, HEADLESS
    import odds_api

    print("=" * 64)
    print("  CS2 BOT: ПРОВЕРКА КЕФОВ БК (Этап 2)")
    print("=" * 64)
    print("Папка запуска:", os.getcwd())
    print("Включены:", ", ".join(ODDS_ENABLED))
    if os.path.isdir("debug_odds"):
        old = [f for f in os.listdir("debug_odds") if f.endswith(".json")]
        if old:
            print(f"! В debug_odds лежат старые дампы ({len(old)} шт.)")
            print("! Лучше удалить их перед тестом, чтобы не путать со свежими.")

    pairs = {}

    def add(bk, plist):
        for p in plist:
            na, nb = odds_api.norm_name(p["team1"]), odds_api.norm_name(p["team2"])
            if na and nb and na != nb:
                pairs.setdefault(tuple(sorted((na, nb))), {})[bk] = p

    # -------- 1. Пари и Фонбет — прямой HTTP (быстро, без браузера)
    step = 1
    total_steps = 2 + sum(1 for b in ("winline", "betboom") if b in ODDS_ENABLED)
    for bk, fn in (("pari", odds_api.fetch_pari), ("fonbet", odds_api.fetch_fonbet)):
        if bk not in ODDS_ENABLED:
            continue
        print(f"\n[{step}/{total_steps}] {bk} (JSON API, без браузера)...")
        step += 1
        try:
            plist = await fn()
            add(bk, plist)
            print(f"   получено матчей: {len(plist)}")
            for p in plist[:5]:
                print(f"     {p['team1'][:26]} vs {p['team2'][:26]}  "
                      f"{p['w1']:.2f}/{p['w2']:.2f}")
        except Exception as e:
            print(f"   ОШИБКА: {e!r}")

    # -------- 2-3. Браузерные БК (Винлайн, Бетбум)
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("\nCamoufox не установлен — браузерные БК пропущены.")
        print("Установи: pip install camoufox[geoip] && python -m camoufox fetch")
        AsyncCamoufox = None

    if AsyncCamoufox is not None:
        async with AsyncCamoufox(persistent_context=True, user_data_dir=PROFILE_DIR,
                                 headless=HEADLESS, humanize=True) as browser:
            page = await browser.new_page()
            for bk in ("winline", "betboom"):
                if bk not in ODDS_ENABLED:
                    continue
                print(f"\n[{step}/{total_steps}] {bk}: открываю страницу линии (~25 сек)...")
                step += 1
                try:
                    fn = {"winline": odds_api.fetch_winline,
                          "betboom": odds_api.fetch_betboom}[bk]
                    plist = await fn(page)
                    add(bk, plist)
                    print(f"   распознано пар: {len(plist)}")
                    for p in plist[:5]:
                        print(f"     {p['team1'][:26]} vs {p['team2'][:26]}  "
                              f"{p['w1']:.2f}/{p['w2']:.2f}")
                except Exception as e:
                    print(f"   ОШИБКА: {e!r}")

    # -------- итог
    print("\n" + "=" * 64)
    print(f"ИТОГО: {len(pairs)} уникальных пар с кефами")
    shown = 0
    for (a, b), per in pairs.items():
        for bk, p in per.items():
            if shown >= 25:
                break
            print(f"   {a} vs {b}  |  П1 {p['w1']:.2f} / П2 {p['w2']:.2f}  ({bk})")
            shown += 1
    n_extra = sum(len(per) for per in pairs.values()) - shown
    if n_extra > 0:
        print(f"   ...и ещё {n_extra} строк")
    print("=" * 64)

    dumps = os.listdir("debug_odds") if os.path.isdir("debug_odds") else []
    if dumps:
        print(f"\nДампов необработанных данных: {len(dumps)} (папка debug_odds/).")
        print("Если какая-то БК дала 0 пар — скинь эти файлы, парсер докрутим.")
    else:
        print("\nДампов нет — все ответы распознались.")

    print("\nГотово. Запускай бота: run_bot.bat")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
