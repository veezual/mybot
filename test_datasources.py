"""
test_datasources.py — диагностика внешних источников данных (V106).

Проверяет на ТВОЕЙ машине:
  1. PandaScore — жив ли ключ, сколько матчей upcoming/running, задержка.
  2. API-Sport — оба ключа: план, остаток квоты, услуги; live-матчи.
  3. Мэтчинг — как команды PandaScore склеиваются с командами HLTV из базы.
  4. Единый формат — сохраняет образец в debug_odds/unified_sample.json.
  5. bo3.gg — новый быстрый источник карт/H2H (без браузера, секунды).

Запуск двойным кликом: test_datasources.bat
(или из консоли: venv\\Scripts\\python.exe test_datasources.py)
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db          # noqa: E402
import config                  # noqa: E402
import sportsdata              # noqa: E402

SEP = "=" * 64


def fmt_num(v):
    return "-" if v is None else str(v)


async def main():
    db.init_db()
    print(SEP)
    print("  CS2 BOT: ПРОВЕРКА ИСТОЧНИКОВ ДАННЫХ (V106)")
    print(SEP)
    print("Папка запуска:", os.getcwd())
    print("PandaScore токен:", "задан" if config.PANDASCORE_TOKEN else "НЕ задан (.env)")
    print("API-Sport ключей:", len(config.APISPORT_TOKENS),
          "(квота/ключ/день:", config.APISPORT_DAILY_LIMIT, ")")
    print("Кросс-проверка:", "вкл" if config.APISPORT_CROSSCHECK else "выкл (экономим квоту)")
    print()

    report = await sportsdata.diagnostics()

    # ---------------------------------------------------------- PandaScore
    ps = report.get("pandascore", {})
    print("-" * 64)
    print("1) PANDASCORE (первичный источник)")
    if not ps:
        print("   ! токен не задан — источник выключен (проверь .env)")
    else:
        print(f"   upcoming (ближайшие): {ps.get('upcoming')} матчей "
              f"за {ps.get('upcoming_latency')} c")
        print(f"   running  (в лайве):   {ps.get('running')} матчей "
              f"за {ps.get('running_latency')} c")
        print(f"   разбор ответа: {'OK' if ps.get('sample_ok') else 'FAIL'}")
        if not ps.get("sample_ok"):
            print("   ! матчи пришли, но не разобрались — пришли мне этот вывод")
    print()

    # ---------------------------------------------------------- API-Sport
    asx = report.get("api_sport", {})
    print("-" * 64)
    print("2) API-SPORT (резервный источник, ротация ключей)")
    for i in (1, 2):
        tail = asx.get(f"key{i}_tail")
        if not tail:
            continue
        if f"key{i}_error" in asx:
            print(f"   ключ ...{tail}: ОШИБКА ({asx[f'key{i}_error']}) — "
                  f"проверь его в кабинете app.api-sport.ru")
        else:
            print(f"   ключ ...{tail}: план «{asx.get(f'key{i}_plan')}», "
                  f"осталось {fmt_num(asx.get(f'key{i}_remaining'))} запр./сутки")
            svc = asx.get(f"key{i}_services") or []
            if svc:
                print(f"      услуги: {', '.join(svc)}")
    print("   Примечание: квота тестового плана мала, поэтому API-Sport")
    print("   вызывается только когда PandaScore недоступен (экономия).")
    print()

    # ---------------------------------------------------------- мэтчинг
    mt = report.get("matcher", {})
    print("-" * 64)
    print("3) МЭТЧИНГ КОМАНД (PandasScore <-> HLTV)")
    print(f"   команд в базе: {mt.get('db_teams')}")
    if mt.get("db_teams", 0) < 5:
        print("   ! база почти пуста — сначала собери «Матчи сегодня» в боте,")
        print("     потом повтори тест: без HLTV-команд мэтчить некому.")
    else:
        print(f"   проверено команд: {mt.get('checked')}, "
              f"сопоставлено: {mt.get('matched_count')}")
        for src, dst, sc in (mt.get("matched_pairs") or [])[:12]:
            print(f"      {src}  ->  {dst}  ({sc})")
        if not mt.get("matched_count"):
            print("   ! ни одной пары — пришли мне этот вывод, поднимем порог")
    print()

    # ---------------------------------------------------------- единый формат
    print("-" * 64)
    print("4) ЕДИНЫЙ ФОРМАТ (unified JSON)")
    try:
        up = await sportsdata.get_upcoming(force=True)
    except Exception as e:
        up = []
        print("   ошибка:", e)
    if up:
        sample_path = os.path.join("debug_odds", "unified_sample.json")
        with open(sample_path, "w", encoding="utf-8") as f:
            json.dump(up[:3], f, ensure_ascii=False, indent=1)
        u = up[0]
        print("   пример:", u["title"], "|", u["status"], "|", u["begin_msk"],
              "|", u["tournament"] or "без турнира")
        print("   ключи:", ", ".join(sorted(u.keys())))
        print(f"   сохранено: {sample_path} (первые 3 матча)")
    else:
        print("   ! список пуст — PandaScore недоступен и API-Sport не помог")
    print()

    # ---------------------------------------------------------- bo3.gg (V106)
    print("-" * 64)
    print("5) BO3.GG (V106: карты + H2H за секунды, без браузера)")
    import bo3data
    bo3_ok = False
    try:
        t0 = time.monotonic()
        maps = await bo3data.get_team_maps("FUT")
        t_maps = time.monotonic() - t0
        print(f"   карты FUT: {len(maps)} шт. за {t_maps:.1f} c —",
              dict(list(maps.items())[:3]))
        t1 = time.monotonic()
        rows = await bo3data.get_h2h("FUT", "Nemiga")
        t_h = time.monotonic() - t1
        print(f"   H2H FUT vs Nemiga: {len(rows)} матчей за {t_h:.1f} c")
        if rows:
            print("   последний:", rows[0])
        bo3_ok = bool(maps)
    except Exception as e:
        print("   ОШИБКА:", e)
    print()

    # ---------------------------------------------------------- итог
    print(SEP)
    ps_ok = bool(ps.get("sample_ok"))
    as_ok = any(f"key{i}_plan" in asx for i in (1, 2))
    print("ИТОГ:")
    print("   PandaScore:", "PASS" if ps_ok else "FAIL")
    print("   API-Sport :", "PASS" if as_ok else "FAIL (бот переживёт — есть резерв)")
    print("   bo3.gg    :", "PASS" if bo3_ok else "FAIL (с ним упадёт скорость — пришли вывод)")
    print("   Мэтчинг   :", "PASS" if mt.get("matched_count") else "СМОТРИ ВЫШЕ")
    print(SEP)
    print("Скопируй ЭТОТ вывод целиком в чат, если что-то FAIL.")


if __name__ == "__main__":
    asyncio.run(main())
    print()
    input("Нажми Enter, чтобы закрыть окно...")
