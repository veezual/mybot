"""
bot.py — Telegram-бот «CS2 Analytics» (aiogram 3).

Меню (V106):
  🎮 Матчи сегодня  — матчи дня: PandaScore (HTTP, ~3 сек) -> HLTV (браузер)
  🔴 Лайв           — идущие сейчас матчи: счёт серии, карта, трансляции
  📊 Команды        — статистика карт по любой команде
  🤝 H2H            — личные встречи двух команд
  💎 VALUE          — скан перевесов модели над рынком
  📜 История        — сигналы value-модели + итоги (ROI), отметка результатов
  🔄 Обновить данные — принудительный свежий сбор

Скорость (V106 — ЭПОХА bo3.gg):
  • карты команд и H2H — HTTP-источник bo3.gg за 2-5 СЕКУНД, без браузера;
  • после загрузки списка матчей карты всех команд дня греются фоном;
  • прогноз по матчу работает даже без HLTV-ID (по именам, через bo3.gg);
  • Camoufox остаётся запасным путём (если bo3.gg недоступен);
  • ответ на клик кнопки уходит мгновенно (фикс 'query is too old').

Доступ ограничен ALLOWED_USER_ID из .env.
"""
import asyncio
import logging
import time
from html import escape as hesc   # экранирование AI-текстов для HTML-режима

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, BufferedInputFile,
                           InlineKeyboardButton, KeyboardButton)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

import config
import database as db
import collector
import sportsdata
import bo3data
import value_model
import ai_assist
import deepstats
import newsdata
import roster_watch
import scorebot
from predict import predict_match, plain_text_reasons, BK_LABELS
from cards import team_card, match_card, h2h_card

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

if not config.TG_TOKEN:
    raise SystemExit("❌ TG_TOKEN не найден в .env!")

bot = Bot(token=config.TG_TOKEN)
dp = Dispatcher()

STATUS_MSG = "⏳ {text}"

DISCLAIMER = ("⚠️ Аналитика, а не финансовый совет. Ставки — риск: "
              "играй ответственно и только на суммы, которые готов потерять.")


# ---------------------------------------------------------------- утилиты

def allowed(user_id: int) -> bool:
    return config.ALLOWED_USER_ID == 0 or user_id == config.ALLOWED_USER_ID


def main_menu() -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="🎮 Матчи сегодня"))
    kb.row(KeyboardButton(text="🏆 Турниры"), KeyboardButton(text="🔴 Лайв"))
    kb.row(KeyboardButton(text="💎 VALUE"), KeyboardButton(text="📊 Команды"))
    kb.row(KeyboardButton(text="🤝 H2H"), KeyboardButton(text="🤖 AI-разбор"))
    kb.row(KeyboardButton(text="📰 Новости"), KeyboardButton(text="📜 История"))
    kb.row(KeyboardButton(text="🔄 Обновить данные"))
    return kb.as_markup(resize_keyboard=True)


async def safe_edit(msg: types.Message, text: str, **kwargs):
    """Правка сообщения с доп. параметрами (например parse_mode="HTML").

    V110: раньше сигнатура не принимала parse_mode — кнопки «AI-разбор» и
    «Новости» падали с TypeError уже ПОСЛЕ того, как ответ был готов.
    Если Telegram не принял параметры (например, битая HTML-разметка в
    тексте) — повторяем тем же текстом без разметки, чтобы ответ не пропал.
    """
    try:
        await msg.edit_text(text, **kwargs)
    except Exception:
        if kwargs:
            try:
                await msg.edit_text(text)
            except Exception:
                pass


async def safe_answer(callback: CallbackQuery, text: str | None = None,
                      show_alert: bool = False):
    """Ответ на клик по inline-кнопке.

    Телеграм «забывает» клик через ~1-2 минуты: если обработчик работал
    дольше (сбор данных в браузере) и ответить в конце — сервер отдаёт
    'query is too old' и падает весь обработчик. Поэтому ЛЮБОЙ ответ
    на callback — только через safe_answer: устаревший клик просто
    молча пропускается, а данные всё равно уйдут сообщением в чат."""
    try:
        if text:
            await callback.answer(text, show_alert=show_alert)
        else:
            await callback.answer()
    except Exception:
        pass


def make_progress(status_msg: types.Message):
    """Единые статусы сбора для матчей/карт/H2H."""
    async def cb(stage: str, i: int, n: int, team: str):
        if stage == "matches":
            await safe_edit(status_msg, "⏳ Открываю hltv.org/matches (10–60 сек)…")
        elif stage == "maps":
            await safe_edit(status_msg, f"⏳ Собираю карты: {team}…")
        elif stage == "h2h":
            await safe_edit(status_msg, f"⏳ Личные встречи: {team}…")
    return cb


async def run_collect(message: types.Message, force: bool = False) -> list[dict] | None:
    """HLTV-сбор матчей (HTTP -> браузер). Возврат: матчи либо None."""
    status = await message.answer(STATUS_MSG.format(text="Собираю матчи с HLTV…"))
    try:
        matches = await collector.collect_matches_only(
            progress_cb=make_progress(status), force=force)
        await safe_edit(status, f"✅ Матчей в базе: {len(matches)}. "
                                "Карты команд соберутся при клике на матч.")
        return matches
    except RuntimeError as e:
        await safe_edit(status, f"❌ {e}")
    except Exception as e:
        log.exception("collect failed")
        await safe_edit(status, f"❌ Сбор не удался: {e}. Попробуй ещё раз через минуту.")
    return None


async def ensure_matches_fresh(message: types.Message, force: bool = False) -> list[dict] | None:
    """ЦЕПОЧКА ИСТОЧНИКОВ матчей (V106): сбор не падает никогда.
      1) PandaScore + добор пар из bo3.gg  (HTTP, ~3 сек)
      2) чистый bo3.gg                     (HTTP, ~1 сек, работает на пустой базе)
      3) HLTV: лёгкий HTTP -> Camoufox     (как в старой версии)
    Возврат: матчи либо None (совсем всё лежит)."""
    matches = db.get_matches()
    age = db.matches_age()
    if not force and matches and age is not None and age <= config.MATCHES_TTL:
        return matches

    try:
        fast = await collector.collect_matches_via_sources()
    except Exception as e:
        log.warning("Быстрый путь: %s", e)
        fast = None
    if fast:
        return fast[0]
    try:
        return await collector.collect_matches_bo3()
    except Exception as e:
        log.warning("bo3-путь: %s", e)
    return await run_collect(message, force=True)


def teams_keyboard(prefix: str, exclude_id: str | None = None,
                   matches: list[dict] | None = None) -> types.InlineKeyboardMarkup:
    """Список команд (2 в ряд) либо пар матчей."""
    kb = InlineKeyboardBuilder()
    if matches is not None:
        for m in matches:
            label = f"{m['team1_name']} vs {m['team2_name']}"
            if m.get("start_time"):
                label += f" · {m['start_time']}"
            kb.row(InlineKeyboardButton(
                text=label[:64],
                callback_data=f"match:{m['team1_id']}:{m['team2_id']}"))
    else:
        teams = db.get_all_teams()
        if exclude_id:
            teams = [t for t in teams if t["hltv_id"] != exclude_id]
        for t in teams:
            kb.button(text=t["name"][:24], callback_data=f"{prefix}:{t['hltv_id']}")
        kb.adjust(2)
        if not teams:
            kb.row(InlineKeyboardButton(text="База пуста — нажми 🔄 Обновить данные",
                                        callback_data="noop"))
    return kb.as_markup()


def history_keyboard(rows: list[dict]) -> types.InlineKeyboardMarkup | None:
    """Кнопки ✅/❌ для незакрытых сигналов."""
    pending = [r for r in rows if r["status"] == "pending"]
    if not pending:
        return None
    kb = InlineKeyboardBuilder()
    for r in pending:
        kb.button(text=f"✅ #{r['id']}", callback_data=f"sig:win:{r['id']}")
        kb.button(text=f"❌ #{r['id']}", callback_data=f"sig:lose:{r['id']}")
    kb.adjust(2)
    return kb.as_markup()


# ---------------------------------------------------------------- команды

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer(
        "👋 <b>CS2 Analytics Bot</b> — аналитика матчей CS2\n\n"
        "🎮 <b>Матчи сегодня</b> — список матчей дня (PandaScore + HLTV)\n"
        "🏆 <b>Турниры</b> — матчи по турнирам: идущие и будущие\n"
        "🔴 <b>Лайв</b> — идущие сейчас: счёт, карта, трансляции\n"
        "📊 <b>Команды</b> — винрейты по картам любой команды\n"
        "🤝 <b>H2H</b> — личные встречи: счёт серии + последние матчи\n"
        "💎 <b>VALUE</b> — перевесы модели над кефами БК\n"
        "🤖 <b>AI-разбор</b> — ИИ-аналитик интерпретирует статистику пары\n"
        "📰 <b>Новости</b> — свежие новости команды (ростер, переходы)\n"
        "📜 <b>История</b> — сигналы и итоги (винрейт, ROI)\n"
        "🔄 <b>Обновить данные</b> — принудительный свежий сбор\n\n"
        "Клик по матчу: прогноз приходит сразу, кефы БК — следом.\n"
        "🔴 Лайв → «Следить» — Лайв-центр: раунды, деньги, закупы в реальном "
        "времени (scorebot HLTV).\n"
        "V113: прогнозы на глубокой статистике HLTV — рейтинги игроков "
        "(rating 2.1, 3 мес), мировой ранг, AI-разбор с обоснованными "
        "ставками (основная/риск/не брать). AI-ключ: /aihelp\n"
        "V115: стабильная статистика (единый namespace при смене источников), "
        "фолбэк HLTV для команд без bo3-истории, честные кулдауны после "
        "сетевых сбоев, защита value-сигналов от неполных данных.\n\n" + DISCLAIMER,
        reply_markup=main_menu(), parse_mode="HTML")


@dp.message(Command("aihelp"))
async def cmd_aihelp(message: Message):
    """V109: инструкция по подключению бесплатного AI-ключа."""
    if not allowed(message.from_user.id):
        return
    status = ("✅ <b>AI-ключ установлен</b> — кнопка 🤖 работает."
              if ai_assist.ai_available() else
              "❌ <b>AI-ключ не задан</b> — AI-разбор пока недоступен.")
    await message.answer(
        "🤖 <b>Подключение бесплатного AI-аналитика</b>\n\n"
        + status + "\n\n"
        "<b>Вариант 1 — Groq (рекомендую: без карты, самый быстрый)</b>\n"
        "1. Зайди на https://console.groq.com/keys (регистрация почтой/Google)\n"
        "2. Create API Key → скопируй\n"
        "3. В .env допиши:\n"
        "<code>AI_API_KEY=gsk_твой_ключ</code>\n"
        "<code>AI_API_BASE=https://api.groq.com/openai/v1</code>\n"
        "<code>AI_MODEL=openai/gpt-oss-120b</code>\n"
        "<i>(llama-3.3-70b-versatile Groq снял с сервиса — не используй её)</i>\n\n"
        "<b>Вариант 2 — Google Gemini (AI Studio)</b>\n"
        "1. https://aistudio.google.com/apikey → Create API key\n"
        "2. В .env:\n"
        "<code>AI_API_KEY=твой_ключ</code>\n"
        "<code>AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai</code>\n"
        "<code>AI_MODEL=gemini-2.5-flash</code>\n\n"
        "<b>Вариант 3 — OpenRouter (много бесплатных моделей)</b>\n"
        "1. https://openrouter.ai/keys\n"
        "2. AI_API_BASE=https://openrouter.ai/api/v1\n\n"
        "После правки .env перезапусти бота. Без ключа всё остальное "
        "работает как раньше — AI опционален.\n\n" + DISCLAIMER,
        parse_mode="HTML")


_last_bg_hltv = 0.0


def start_bg_hltv_refresh():
    """HLTV-сверка списка матчей в фоне: не блокирует, обновит базу сама.
    V108: не чаще раза в BG_HLTV_MIN_INTERVAL — раньше каждый клик по кнопке
    и каждый ps:-матч запускали полный цикл (HTTP 403 -> Camoufox -> минута
    работы и куча ошибок в логах впустую)."""
    global _last_bg_hltv
    if time.time() - _last_bg_hltv < config.BG_HLTV_MIN_INTERVAL:
        return None
    _last_bg_hltv = time.time()

    async def _task():
        try:
            ms = await collector.collect_matches_only(force=True)
            log.info("Фоновый HLTV-сбор завершён: %d матчей", len(ms or []))
        except Exception as e:
            log.warning("Фоновый HLTV-сбор: %s", e)
    return asyncio.create_task(_task())


@dp.message(F.text.regexp(r"^\s*🎮\s*Матчи"))
async def show_matches(message: Message):
    if not allowed(message.from_user.id):
        return
    matches = await ensure_matches_fresh(message)
    if not matches:
        return
    src = db.get_meta("last_matches_source", "")
    label = {"pandascore+bo3": "PandaScore + bo3.gg",
             "bo3": "bo3.gg", "hltv_http": "HLTV",
             "hltv_browser": "HLTV"}.get(src, "PandaScore + bo3.gg")
    unresolved_note = ""
    try:
        from odds_api import norm_name as _nn
        have_hltv = sum(1 for m in matches
                        if not str(m["team1_id"]).startswith(("b", "ps:")))
        if have_hltv < len(matches):
            unresolved_note = (f"\n<i>{len(matches) - have_hltv} матчей без HLTV-сопоставления — "
                               f"прогноз по ним работает через bo3.gg.</i>")
    except Exception:
        pass
    await message.answer(
        f"🗓 <b>Матчи на сегодня</b> (источник: {label})" + unresolved_note +
        "\nВыбери пару для прогноза:",
        parse_mode="HTML", reply_markup=teams_keyboard("x", matches=matches))
    collector.kick_warmup()
    collector.kick_h2h_warmup()
    roster_watch.kick_roster_watch()   # V109: проверка составов в фоне
    deepstats.kick_deep_warmup()       # V113: рейтинги игроков + топ HLTV
    start_bg_hltv_refresh()


@dp.message(F.text.regexp(r"^\s*📊\s*Команды"))
async def show_teams(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer("Выбери команду:", reply_markup=teams_keyboard("team"))


@dp.message(F.text.regexp(r"^\s*🤝\s*H2H"))
async def show_h2h(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer("Шаг 1/2: выбери первую команду:",
                         reply_markup=teams_keyboard("h2h1"))


@dp.message(F.text.regexp(r"^\s*💎\s*VALUE"))
async def show_value(message: Message):
    if not allowed(message.from_user.id):
        return
    # матчи должны быть в базе (цепочка источников, без падений)
    matches = await ensure_matches_fresh(message)
    if not matches:
        return

    status = await message.answer(STATUS_MSG.format(text="Готовлю value-скан…"))

    # 1) свежие кефы: если общий сбор старше VALUE_ODDS_TTL — пересобрать
    raw_last = db.get_meta("last_odds_sweep", "")
    sweep_age = (time.time() - float(raw_last)) if raw_last else None
    if sweep_age is None or sweep_age >= config.VALUE_ODDS_TTL:
        await safe_edit(status, "⏳ Собираю свежие кефы со всех БК (до 2 мин)…")
        try:
            await collector.odds_sweep(
                status_cb=lambda txt: safe_edit(status, "⏳ " + txt))
        except Exception as e:
            log.warning("odds_sweep: %s", e)
            await safe_edit(status, "⚠️ Кефы собрать не удалось — сканирую по старым.")

    # 2) скан по кэшу — быстро, без браузера
    await safe_edit(status, "⏳ Считаю модель и сравниваю с рынком…")
    try:
        signals, skipped = value_model.scan_matches(matches)
    except Exception as e:
        log.exception("value scan failed")
        await safe_edit(status, f"❌ Скан не удался: {e}")
        return
    await safe_edit(status, value_model.format_scan_result(signals, skipped))


@dp.message(F.text.regexp(r"^\s*📜\s*История"))
async def show_history(message: Message):
    if not allowed(message.from_user.id):
        return
    rows = db.get_signals(limit=10)
    stats = db.signals_stats()
    await message.answer(value_model.format_history(rows, stats),
                         parse_mode="HTML", reply_markup=history_keyboard(rows))


@dp.message(F.text.regexp(r"^\s*🔄\s*Обновить"))
async def refresh_data(message: Message):
    if not allowed(message.from_user.id):
        return
    status = await message.answer("⏳ Обновляю список матчей…")
    matches = None
    try:
        matches = await ensure_matches_fresh(message, force=True)
    except Exception as e:
        await safe_edit(status, f"❌ Сбор не удался: {e}. Попробуй ещё раз через минуту.")
        return
    if matches:
        src = db.get_meta("last_matches_source", "")
        label = {"pandascore+bo3": "PandaScore + bo3.gg", "bo3": "bo3.gg",
                 "hltv_http": "HLTV", "hltv_browser": "HLTV"}.get(src, "источники")
        await safe_edit(status, f"✅ Список обновлён ({label}). "
                                "Прогрев карт/формы и H2H идут в фоне.")
        collector.kick_warmup()
        collector.kick_h2h_warmup()
        roster_watch.kick_roster_watch()   # V109
        deepstats.kick_deep_warmup()       # V113
        start_bg_hltv_refresh()
        await message.answer("🗓 Выбери пару для прогноза:",
                             reply_markup=teams_keyboard("x", matches=matches))
    else:
        await safe_edit(status, "❌ Все источники недоступны. Попробуй через пару минут.")


# ---------------------------------------------------------------- лайв (V105)

@dp.message(F.text.regexp(r"^\s*🔴\s*Лайв"))
async def show_live(message: Message):
    if not allowed(message.from_user.id):
        return
    status = await message.answer("⏳ Смотрю, что идёт прямо сейчас…")
    live = []
    try:
        live = await sportsdata.get_live()
    except Exception as e:
        log.warning("live: %s", e)
    if not live:
        # V106: фолбэк — лайв с bo3.gg (HTTP, ~1 сек)
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            now = _dt.now(_ZI(config.TZ_NAME)).strftime("%Y-%m-%d")
            bl, _ = await bo3data.live_matches(now)
        except Exception as e:
            log.warning("live bo3: %s", e)
            bl = []
        if not bl:
            await safe_edit(status,
                            "Сейчас идущих матчей CS2 не найдено.\n"
                            "Попробуй ещё раз через пару минут — список живой.")
            return
        lines = ["🔴 <b>Идут сейчас</b> (источник: bo3.gg)", ""]
        for m in bl[:12]:
            s1, s2 = m.get("s1"), m.get("s2")
            s = f"{s1}:{s2}" if s1 is not None and s2 is not None else "—:—"
            lines.append(f"• <b>{m['n1']} vs {m['n2']}</b> — {s} · BO{m.get('bo') or '?'}")
        lines += ["", "Кликни матч в «Матчи сегодня», когда он начнётся — прогноз сработает и в лайве."]
        await safe_edit(status, "\n".join(lines))
        return
    lines = ["🔴 <b>Идут сейчас</b>", ""]
    kb = InlineKeyboardBuilder()
    for u in live[:10]:
        sc = u.get("series_score")
        s = f"{sc[0]}:{sc[1]}" if sc else "—:—"
        gt = u.get("games_total")
        mapinfo = f" · карта {u.get('current_game')}/{gt}" if gt else ""
        tour = f" · {u['tournament']}" if u.get("tournament") else ""
        lines.append(f"• <b>{u['title']}</b> — {s}{mapinfo}{tour}")
        kb.row(InlineKeyboardButton(text=u["title"][:60],
                                    callback_data=f"live:{u['uid']}"))
    kb.adjust(1)
    lines += ["", "Нажми на матч — подробности и трансляции."]
    try:
        await status.edit_text("\n".join(lines), parse_mode="HTML",
                               reply_markup=kb.as_markup())
    except Exception:
        await message.answer("\n".join(lines), parse_mode="HTML",
                             reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("live:"))
async def cb_live(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    uid = callback.data.split(":", 1)[1]
    try:
        live = await sportsdata.get_live()
    except Exception:
        live = []
    u = next((x for x in live if x.get("uid") == uid), None)
    if not u:
        await safe_answer(callback, "Матч уже не в лайве — обнови список",
                          show_alert=True)
        return
    status_txt = {"live": "идёт сейчас", "not_started": "не начался",
                  "finished": "завершён"}.get(u.get("status"), u.get("status"))
    lines = [f"🔴 <b>{u['title']}</b>", ""]
    if u.get("tournament"):
        lines.append(f"Турнир: {u['tournament']}")
    if u.get("begin_msk"):
        lines.append(f"Начало: {u['begin_msk']} (МСК)")
    lines.append(f"Статус: {status_txt}")
    sc = u.get("series_score")
    if sc:
        lines.append(f"Счёт по картам: <b>{sc[0]}:{sc[1]}</b>")
    if u.get("games_total"):
        lines.append(f"Текущая карта: {u.get('current_game') or '?'} из {u['games_total']}")
    streams = u.get("streams") or []
    if streams:
        lines.append("")
        lines.append("Трансляции:")
        for s in streams[:3]:
            lang = (s["lang"] or "?").upper()
            mark = " (основная)" if s.get("main") else ""
            lines.append(f"• {lang}{mark}: {s['url']}")
    lines.append("")
    lines.append("Источники: " + ", ".join(u.get("sources") or []))
    lines.append("")
    lines.append(DISCLAIMER)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔴 Следить (Лайв-центр)",
                                callback_data=f"watch:{u['uid']}"))
    kb.row(InlineKeyboardButton(text="⭐ Дайджест счёта в чат",
                                callback_data=f"follow:{u['uid']}"))
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  reply_markup=kb.as_markup())
    await safe_answer(callback)


# ---------------------------------------------------------------- колбэки

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await safe_answer(callback, "Сначала собери данные кнопкой 🔄",
                      show_alert=True)


@dp.callback_query(F.data.startswith("sig:"))
async def cb_signal_result(callback: CallbackQuery):
    """Отметка результата сигнала: зашло / не зашло."""
    if not allowed(callback.from_user.id):
        return
    try:
        _, verdict, sig_id = callback.data.split(":")
        if verdict not in ("win", "lose"):
            raise ValueError(verdict)
    except Exception:
        await safe_answer(callback, "Некорректная отметка", show_alert=True)
        return
    db.set_signal_result(sig_id, verdict)
    rows = db.get_signals(limit=10)
    stats = db.signals_stats()
    try:
        await callback.message.edit_text(value_model.format_history(rows, stats),
                                         parse_mode="HTML")
    except Exception:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=history_keyboard(rows))
    except Exception:
        pass
    await safe_answer(
        callback, "Отмечено: " + ("зашло ✅" if verdict == "win" else "не зашло ❌"))


@dp.callback_query(F.data.startswith("team:"))
async def cb_team(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    team_id = callback.data.split(":")[1]
    team = db.get_team(team_id)
    if not team:
        await safe_answer(callback, "Команда не найдена — обнови базу",
                          show_alert=True)
        return
    # Гасим «часики» кнопки сразу: сбор карт может идти дольше жизни клика.
    await safe_answer(callback)

    await callback.message.answer("⏳ Проверяю свежесть данных по картам…")
    maps = await collector.ensure_maps(team_id, team["name"])
    if not maps:
        await callback.message.answer("❌ Не удалось получить карты. Попробуй ещё раз.")
        return

    photo = team_card(team["name"], team["logo_url"], maps)
    best = max(maps.items(), key=lambda x: x[1])
    worst = min(maps.items(), key=lambda x: x[1])
    await callback.message.answer_photo(
        BufferedInputFile(photo, filename="team.png"),
        caption=f"🗺 <b>{team['name']}</b>\n"
                f"Лучшая карта: <b>{best[0]}</b> — {best[1]:.1f}%\n"
                f"Худшая: <b>{worst[0]}</b> — {worst[1]:.1f}%",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("h2h1:"))
async def cb_h2h_step1(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    team_id = callback.data.split(":")[1]
    team = db.get_team(team_id)
    if not team:
        await safe_answer(callback, "Не найдено", show_alert=True)
        return
    await callback.message.answer(
        f"Шаг 2/2: выбери соперника для H2H с <b>{team['name']}</b>:",
        parse_mode="HTML", reply_markup=teams_keyboard(f"h2h2:{team_id}"))
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("h2h2:"))
async def cb_h2h_step2(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    _, id1, id2 = callback.data.split(":")
    t1, t2 = db.get_team(id1), db.get_team(id2)
    if not t1 or not t2:
        await safe_answer(callback, "Команды не найдены", show_alert=True)
        return
    await safe_answer(callback)  # часики — долой, дальше долгий сбор

    status = await callback.message.answer(
        f"⏳ Ищу личные встречи {t1['name']} vs {t2['name']}…")
    try:
        rows = await collector.ensure_h2h(id1, id2)
    except Exception as e:
        await safe_edit(status, f"❌ Ошибка сбора H2H: {e}")
        return

    await safe_edit(status, f"🤝 Найдено матчей: {len(rows)}")
    summary = predict_match(t1["name"], {}, t2["name"], {}, rows)["h2h"]
    photo = h2h_card(t1["name"], t1["logo_url"], t2["name"], t2["logo_url"], summary)
    await callback.message.answer_photo(
        BufferedInputFile(photo, filename="h2h.png"),
        caption=f"🤝 <b>{t1['name']} vs {t2['name']}</b>: серия "
                f"<b>{summary['w1']}:{summary['w2']}</b>",
        parse_mode="HTML")


async def odds_followup(chat_id: int, id1: str, id2: str, name1: str, name2: str):
    """Фоновая догрузка кефов (V105): приходит отдельным сообщением после карточки."""
    try:
        import odds_api
        raw = await collector.ensure_odds(id1, id2, name1, name2)
        if not raw:
            await bot.send_message(chat_id, "Кефов на эту пару в линиях БК не нашлось.")
            return
        aligned = odds_api.align_odds(raw, name1, name2)
        if not aligned:
            await bot.send_message(chat_id, "Кефы получены, но не сопоставились с командами.")
            return
        maps1 = db.get_maps_any(id1)[0]
        maps2 = db.get_maps_any(id2)[0]
        rows = db.get_h2h_any(id1, id2)
        pred = predict_match(name1, maps1, name2, maps2, rows, odds=aligned,
                             form1=db.get_form_any(id1), form2=db.get_form_any(id2))
        ins = pred.get("odds") or {}
        lines = [f"📊 <b>Кефы БК</b> · {name1} vs {name2}", ""]
        for row in ins.get("rows", []):
            best = ""
            if row.get("best") == 1:
                best = f"  ← лучший на {name1}"
            elif row.get("best") == 2:
                best = f"  ← лучший на {name2}"
            lines.append(f"• {row['label']}: {row['k1']:.2f} / {row['k2']:.2f}{best}")
        if ins.get("implied1") is not None:
            lines.append("")
            lines.append(f"Рынок: {ins['implied1']:.0f}% / {ins['implied2']:.0f}% (без маржи)")
        pick = pred.get("pick")
        if pick and ins.get("implied1") is not None:
            edge = ins.get("edge1") or 0.0
            edge_pick = edge if pick == name1 else -edge
            stance = ins.get("pick_stance")
            if not stance:  # совместимость со старым форматом pred
                stance = ("value" if edge_pick >= 3
                          else "caution" if edge_pick <= -3 else "agree")
            if stance == "divergence":
                verdict = (f"⚠️ Расхождение с рынком: модель "
                           f"{ins.get('model_prob_pick', 0):.0f}% против "
                           f"{ins.get('market_prob_pick', 0):.0f}% у БК — пик рискованный")
            elif stance == "value":
                verdict = f"VALUE: модель оценивает {pick} выше рынка на {edge_pick:.0f} п.п."
            elif stance == "caution":
                verdict = f"Осторожно: рынок грузит {pick} сильнее модели ({edge_pick:.0f} п.п.)"
            else:
                verdict = "Перевеса нет — модель и рынок согласны."
            best_side = ins.get("best1") if pick == name1 else ins.get("best2")
            ktxt = ""
            if best_side and best_side[1]:
                ktxt = (f" Лучший кеф на {pick}: {best_side[1]:.2f} "
                        f"({BK_LABELS.get(best_side[0], best_side[0])}).")
            lines.append("")
            lines.append(f"Прогноз: <b>{pick}</b> · уверенность {pred.get('confidence')}%. "
                         f"{verdict}{ktxt}")
        lines.append("")
        lines.append(DISCLAIMER)
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.warning("odds followup: %s", e)


@dp.callback_query(F.data.startswith("match:"))
async def cb_match(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    _, id1, id2 = callback.data.split(":")
    # Матчи из быстрого пути могут прийти без HLTV-ID (ps:...). V106: прогноз
    # всё равно строится — карты/H2H берутся с bo3.gg по именам команд, а
    # HLTV-ID уточняется фоном (нужен для кефов русских БК).
    if str(id1).startswith("ps:") or str(id2).startswith("ps:"):
        start_bg_hltv_refresh()
    matches = db.get_matches()
    match = next((m for m in matches if m["team1_id"] == id1 and m["team2_id"] == id2), None)
    if not match:
        await safe_answer(callback, "Матч не найден — обнови данные",
                          show_alert=True)
        return
    # КЛЮЧЕВОЙ фикс V105.1: гасим «часики» кнопки СРАЗУ. Телеграм живёт
    # кликом ~1-2 минуты, а сбор карт/H2H в браузере идёт дольше —
    # раньше ответ летел в конце и падал с 'query is too old'.
    await safe_answer(callback)

    t1, t2 = db.get_team(id1), db.get_team(id2)
    name1, name2 = match["team1_name"], match["team2_name"]

    # -------- кефы: сначала ТОЛЬКО кэш (мгновенно); полный сбор — фоном, после карточки
    aligned = None
    try:
        import odds_api
        key = ":".join(sorted([str(id1), str(id2)]))
        cached = db.get_odds(key, config.ODDS_TTL)
        if cached:
            aligned = odds_api.align_odds(cached, name1, name2)
    except Exception as e:
        log.warning("odds cache: %s", e)

    status = await callback.message.answer(
        f"⏳ Готовлю прогноз {name1} vs {name2}…")
    try:
        maps1, maps2, rows = await collector.ensure_match_data(
            id1, name1, id2, name2, progress_cb=make_progress(status))
    except Exception as e:
        await safe_edit(status, f"⚠️ Часть данных собрать не удалось ({e}). "
                                "Пробую прогноз на том, что есть…")
        maps1, maps2, rows = db.get_maps_any(id1)[0], db.get_maps_any(id2)[0], db.get_h2h_any(id1, id2)

    if not maps1 and not maps2 and not rows:
        await safe_edit(status, "⚠️ Данные по командам недоступны (bo3.gg и HLTV не ответили). "
                                "Попробуй через пару минут или 🔄 Обновить данные.")
        return

    # V106: жадная модель — карта + форма + H2H; чего нет — учитывается остальное
    # V107: форма/карты берутся по алиасам id (get_form_any/get_maps_any)
    # V109: шринкейдж малых выборок (games) + свежие смены ростера (alerts)
    # V113: глубокие данные HLTV — только из кэша (прогрев кладёт их заранее);
    # сети здесь нет, прогноз остаётся быстрым
    ps1 = deepstats.cached_player_stats(id1)
    ps2 = deepstats.cached_player_stats(id2)
    rk1 = deepstats.rank_for(name1)
    rk2 = deepstats.rank_for(name2)
    pred = predict_match(name1, maps1, name2, maps2, rows, odds=aligned,
                         form1=db.get_form_any(id1), form2=db.get_form_any(id2),
                         games1=db.get_map_games_any(id1),
                         games2=db.get_map_games_any(id2),
                         alert1=db.get_roster_alert_any(id1, config.ROSTER_ALERT_DAYS),
                         alert2=db.get_roster_alert_any(id2, config.ROSTER_ALERT_DAYS),
                         players1=ps1, players2=ps2, rank1=rk1, rank2=rk2)
    # V112: составы (кэш БД -> PandaScore), максимум 10 сек — прогноз не тормозим
    rosters = None
    try:
        r1, r2 = await asyncio.wait_for(asyncio.gather(
            roster_watch.ensure_roster(id1, name1),
            roster_watch.ensure_roster(id2, name2)), timeout=10)
        if r1 or r2:
            rosters = (r1, r2)
    except Exception as e:
        log.info("составы: %s", e)

    await safe_edit(status, "✅ Прогноз готов!")
    photo = match_card(name1, t1["logo_url"] if t1 else None, maps1,
                       name2, t2["logo_url"] if t2 else None, maps2,
                       pred, match.get("start_time", ""), rosters=rosters,
                       players=(ps1, ps2), ranks=(rk1, rk2))

    import re as _re
    reasons_txt = plain_text_reasons(pred)
    caption = (_re.sub(r"</?b>", "", pred["verdict"]) + "\n\n" + reasons_txt
               + "\n\n" + DISCLAIMER)
    if rosters:
        caption += ("\n\n👤 Составы (PandaScore):\n"
                    f"{name1}: " + ", ".join(rosters[0][:5]) + "\n"
                    f"{name2}: " + ", ".join(rosters[1][:5]))
    # V113: звёзды матча в подписи (если рейтинги уже в кэше)
    pinfo = pred.get("players_info") or {}
    stars = []
    for nm, pi in ((name1, pinfo.get("p1")), (name2, pinfo.get("p2"))):
        if pi and pi.get("best"):
            stars.append(f"{pi['best'][0]} ({pi['best'][1]:.2f}) — {nm}")
    if stars:
        caption += "\n⭐ Решающие: " + " · ".join(stars)
    caption = caption[:1024]
    # V109: кнопка AI-разбор прямо на карточке прогноза
    ai_kb = InlineKeyboardBuilder()
    ai_kb.row(InlineKeyboardButton(text="🧠 AI-разбор пары",
                                   callback_data=f"aimatch:{id1}:{id2}"))
    await callback.message.answer_photo(
        BufferedInputFile(photo, filename="match.png"),
        caption=caption, reply_markup=ai_kb.as_markup())

    # -------- кефы фоном (V105): не задерживают карточку прогноза
    if not aligned:
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        asyncio.create_task(odds_followup(chat_id, id1, id2, name1, name2))


# ------------------------------------------------------- AI-разбор и новости (V109)

def _matches_keyboard(callback_prefix: str, matches: list[dict]) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in matches[:14]:
        label = f"{m['team1_name']} vs {m['team2_name']}"
        if m.get("start_time"):
            label += f" · {m['start_time']}"
        kb.row(InlineKeyboardButton(
            text=label[:64],
            callback_data=f"{callback_prefix}:{m['team1_id']}:{m['team2_id']}"))
    return kb.as_markup()


@dp.message(F.text.regexp(r"^\s*🤖\s*AI"))
async def show_ai(message: Message):
    if not allowed(message.from_user.id):
        return
    matches = await ensure_matches_fresh(message)
    if not matches:
        return
    if ai_assist.ai_available():
        head = "🧠 <b>AI-разбор</b>: выбери пару — ИИ проанализирует статистику:"
    else:
        head = ("🧠 <b>AI-разбор</b>: AI-ключ ещё не задан (/aihelp — как получить "
                "бесплатный за 2 минуты).\nБез ключа пришлю обычный текстовый "
                "разбор из базы.\n\nВыбери пару:")
    await message.answer(head, parse_mode="HTML",
                         reply_markup=_matches_keyboard("aimatch", matches))


def _fallback_analysis(facts: dict) -> str:
    """Текстовый разбор без AI — из вердикта модели (чтобы кнопка не молчала)."""
    pred = facts.get("pred") or {}
    lines = ["📊 <b>Разбор из базы (без AI)</b>", ""]
    if pred.get("verdict"):
        lines.append(pred["verdict"])
    for r in pred.get("reasons", [])[:8]:
        lines.append("• " + r)
    return "\n".join(lines)[:3500]


@dp.callback_query(F.data.startswith("aimatch:"))
async def cb_ai_match(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    _, id1, id2 = callback.data.split(":")
    matches = db.get_matches()
    match = next((m for m in matches if m["team1_id"] == id1 and m["team2_id"] == id2), None)
    name1 = match["team1_name"] if match else (db.get_team(id1)["name"] if db.get_team(id1) else id1)
    name2 = match["team2_name"] if match else (db.get_team(id2)["name"] if db.get_team(id2) else id2)
    await safe_answer(callback)   # часики долой — сразу
    status = await callback.message.answer(
        f"🧠 Собираю факты по {name1} vs {name2} и спрашиваю ИИ…")

    # V113: глубокие данные HLTV (рейтинги игроков + мировой ранг) —
    # здесь И ДОГРУЖАЕМ, если кэш пуст/старый. AI-кнопке можно быть медленной:
    # пользователь сам ждёт полный разбор, статусы показываем.
    try:
        async def _deep_status(txt: str):
            await safe_edit(status, txt)
        await asyncio.wait_for(
            deepstats.ensure_deep_pair(id1, name1, id2, name2,
                                       status_cb=_deep_status),
            timeout=120)
    except asyncio.TimeoutError:
        await safe_edit(status, "⚠️ Глубокие данные HLTV не доехали (таймаут) — "
                                "разбор пойдёт по тому, что есть.")
    except Exception as e:
        log.info("deep pair %s vs %s: %s", name1, name2, e)

    await safe_edit(status, f"🧠 Факты собраны. Спрашиваю ИИ…")
    try:
        facts = ai_assist.collect_match_facts(id1, name1, id2, name2)
    except Exception as e:
        log.exception("ai facts failed")
        await safe_edit(status, f"❌ Не удалось собрать факты: {e}")
        return

    answer = await ai_assist.match_analysis(facts)
    if answer:
        text = f"🧠 <b>AI-разбор: {name1} vs {name2}</b>\n\n{hesc(answer)}"
    elif not ai_assist.ai_available():
        text = (_fallback_analysis(facts)
                + "\n\n<i>Это разбор без AI. Хочешь живой анализ текстом? "
                  "Получи бесплатный ключ: /aihelp</i>")
    else:
        text = ("⚠️ AI не ответил (лимит/сеть/таймаут). Вот разбор из базы:\n\n"
                + _fallback_analysis(facts))
    await safe_edit(status, text, parse_mode="HTML")


@dp.message(F.text.regexp(r"^\s*📰\s*Новости"))
async def show_news(message: Message):
    if not allowed(message.from_user.id):
        return
    matches = db.get_matches()
    teams = {}
    for m in matches:
        for tid, nm in ((m["team1_id"], m["team1_name"]),
                        (m["team2_id"], m["team2_name"])):
            if nm and nm not in teams:
                teams[nm] = str(tid)
    if not teams:
        await message.answer("Сначала собери матчи кнопкой 🎮 — новости ищутся "
                             "по командам дня.")
        return
    kb = InlineKeyboardBuilder()
    for nm in sorted(teams)[:16]:
        kb.button(text=nm[:28], callback_data=f"news:{teams[nm]}:{nm}")
    kb.adjust(2)
    await message.answer("📰 Выбери команду — соберу свежие новости "
                         "(ростер, переходы, дисквалификации):",
                         reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("news:"))
async def cb_news(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    _, team_id, team_name = callback.data.split(":", 2)
    await safe_answer(callback)
    if not newsdata.ddgs_available():
        await callback.message.answer(
            "❌ Поиск новостей недоступен: не установлен пакет ddgs.\n"
            "Ставится одной командой: <code>pip install ddgs</code>",
            parse_mode="HTML")
        return
    status = await callback.message.answer(f"📰 Ищу новости: {team_name}…")
    items = await newsdata.search_news(f"{team_name} CS2")
    if not items:
        await safe_edit(status, f"Свежих новостей по {team_name} не нашлось "
                                "(или поиск временно недоступен). Попробуй позже.")
        return
    summary = await ai_assist.news_summary(team_name, items)
    if summary:
        text = f"📰 <b>{team_name} — сводка</b>\n\n{hesc(summary)}"
    elif not ai_assist.ai_available():
        lines = [f"📰 <b>{team_name} — свежие новости</b> "
                 f"(AI-сводка недоступна, ключ: /aihelp)", ""]
        for it in items[:8]:
            dt = (it.get("date") or "")[:10]
            lines.append(f"• {hesc(it.get('title') or '')}"
                         + (f" ({dt})" if dt else ""))
            if it.get("body"):
                lines.append("  " + hesc(it["body"][:200]))
            if it.get("url"):
                lines.append("  " + hesc(it["url"]))
        text = "\n".join(lines)[:3500]
    else:
        text = "⚠️ AI не ответил — попробуй ещё раз через минуту."
    await safe_edit(status, text, parse_mode="HTML")


# --------------------------------- Турниры и Лайв-центр (V111)

_LIVE_AI_SYSTEM = (
    "Ты — комментатор-аналитик CS2. Тебе дают живые события матча из "
    "scorebot HLTV: счёт по раундам, деньги команд, типы закупов, серии, "
    "последние киллы.\n"
    "Правила: только факты из данных, без выдумок; по-русски; до 100 слов; "
    "объясни картину (кто давит и почему), отметь экономику (эко/форс/фулл) "
    "и моментум; в конце — на что смотреть в следующих раундах."
)

_tour_order: list[str] = []          # порядок турниров для callback tour:<idx>
_watch_tasks: dict[str, dict] = {}   # key -> {task, mid}


def _pair_key(n1: str, n2: str) -> str:
    from odds_api import norm_name
    return "|".join(sorted([norm_name(n1 or ""), norm_name(n2 or "")]))


def _now_hms() -> str:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    return _dt.now(_ZI(config.TZ_NAME)).strftime("%H:%M:%S")


async def _live_pairs() -> dict[str, dict]:
    """Идущие матчи: ключ пары имён -> unified-запись (кэш 75 сек)."""
    try:
        live = await sportsdata.get_live()
    except Exception:
        live = []
    out: dict[str, dict] = {}
    for u in live:
        teams = u.get("teams") or []
        names = [(t or {}).get("name", "") for t in teams]
        if len(names) != 2 or not all(names):
            parts = (u.get("title") or "").split(" vs ")
            names = parts if len(parts) == 2 else []
        if len(names) == 2:
            out[_pair_key(names[0], names[1])] = u
    return out


def _tournament_groups(matches: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for m in matches:
        t = (m.get("tournament") or "").strip() or "Другие турниры"
        groups.setdefault(t, []).append(m)
    return groups


async def _send_tournaments(message: Message):
    """Тело кнопки 🏆 Турниры (без проверки allowed — вызывающий уже проверил)."""
    matches = await ensure_matches_fresh(message)
    if not matches:
        return
    live = await _live_pairs()
    groups = _tournament_groups(matches)

    global _tour_order
    ordered = sorted(
        groups,
        key=lambda t: (
            -sum(1 for m in groups[t]
                 if _pair_key(m["team1_name"], m["team2_name"]) in live),
            -len(groups[t]),
        ),
    )
    main_part = ordered[:config.TOURNAMENTS_MAX]
    rest = ordered[config.TOURNAMENTS_MAX:]
    if rest:
        groups["Другие турниры"] = [m for t in rest for m in groups[t]]
        main_part.append("Другие турниры")
    _tour_order = main_part

    lines = ["🏆 <b>Турниры сегодня</b>", ""]
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(main_part):
        ms = groups[t]
        live_n = sum(1 for m in ms
                     if _pair_key(m["team1_name"], m["team2_name"]) in live)
        badge = f"🔴{live_n} · " if live_n else ""
        lines.append(f"• {t}: {len(ms)} матчей"
                     + (f" (🔴 {live_n} идут)" if live_n else ""))
        kb.button(text=f"{badge}{t[:40]}", callback_data=f"tour:{i}")
    kb.adjust(1)
    await message.answer("\n".join(lines)[:3800], parse_mode="HTML",
                         reply_markup=kb.as_markup())


@dp.message(F.text.regexp(r"^\s*🏆\s*Турниры"))
async def show_tournaments(message: Message):
    if not allowed(message.from_user.id):
        return
    await _send_tournaments(message)


@dp.callback_query(F.data.startswith("tour:"))
async def cb_tour(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    try:
        idx = int(callback.data.split(":")[1])
        tour = _tour_order[idx]
    except Exception:
        await safe_answer(callback, "Список устарел — нажми 🏆 Турниры",
                          show_alert=True)
        return
    await safe_answer(callback)
    groups = _tournament_groups(db.get_matches())
    ms = groups.get(tour) or []
    live = await _live_pairs()

    def _is_live(m):
        return _pair_key(m["team1_name"], m["team2_name"]) in live

    live_ms = [m for m in ms if _is_live(m)]
    future_ms = [m for m in ms if not _is_live(m)]

    lines = [f"🏆 <b>{tour}</b>", ""]
    kb = InlineKeyboardBuilder()
    if live_ms:
        lines.append("🔴 <b>Идут сейчас:</b>")
        for m in live_ms[:8]:
            u = live[_pair_key(m["team1_name"], m["team2_name"])]
            sc = u.get("series_score")
            s = f"{sc[0]}:{sc[1]}" if sc else "идёт"
            lines.append(f"• {m['team1_name']} vs {m['team2_name']} — {s}")
            kb.row(InlineKeyboardButton(
                text=f"🔴 Следить: {m['team1_name']} vs {m['team2_name']}"[:64],
                callback_data=f"watch:{u['uid']}"))
        lines.append("")
    future_ms.sort(key=lambda m: (not m.get("start_time"),
                                  m.get("start_time") or ""))
    if future_ms:
        lines.append("⏳ <b>Скоро:</b>")
        for m in future_ms[:12]:
            st_time = m.get("start_time") or ""
            label = f"{m['team1_name']} vs {m['team2_name']}"
            lines.append("• " + label + (f" — {st_time}" if st_time else ""))
            kb.row(InlineKeyboardButton(
                text=(label + (f" · {st_time}" if st_time else ""))[:64],
                callback_data=f"match:{m['team1_id']}:{m['team2_id']}"))
    kb.row(InlineKeyboardButton(text="⬅️ К списку турниров", callback_data="tours"))
    await callback.message.answer("\n".join(lines)[:3800], parse_mode="HTML",
                                  reply_markup=kb.as_markup())


@dp.callback_query(F.data == "tours")
async def cb_tours_back(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    await safe_answer(callback)
    # V111fix: было show_tournaments(callback.message) — внутри снова
    # проверялся allowed(message.from_user.id), а у сообщения бота
    # from_user = сам бот => кнопка «Назад» молча не работала.
    await _send_tournaments(callback.message)


# ------------------------------------------------------- Лайв-центр

def _watch_kb(key: str) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏹ Стоп слежение",
                                callback_data=f"livestop:{key}"))
    kb.row(InlineKeyboardButton(text="🤖 Что происходит? (AI)",
                                callback_data=f"watchai:{key}"))
    kb.row(InlineKeyboardButton(text="⭐ Дайджест счёта в чат",
                                callback_data=f"follow:{key}"))
    return kb.as_markup()


def _poll_card(u: dict, name1: str, name2: str,
               note: str = "") -> str:
    sc = u.get("series_score")
    s = f"{sc[0]}:{sc[1]}" if sc else "—:—"
    gt = u.get("games_total")
    mapinfo = f" · карта {u.get('current_game')}/{gt}" if gt else ""
    if not note:
        note = ("Матч не найден на HLTV (имя не слинковалось) — "
                "эконом-данные недоступны.")
    return (
        f"🔴 <b>ЛАЙВ-ЦЕНТР</b> (опрос источников) · {name1} vs {name2}\n"
        f"Счёт по картам: <b>{s}</b>{mapinfo}\n\n"
        f"<i>{note} Счёт обновляется раз в {config.LIVE_WATCH_POLL} сек.\n"
        f"{_now_hms()}</i>"
    )


async def _live_watch_task(key: str, name1: str, name2: str,
                           status_msg: types.Message):
    """Слежение за матчем: scorebot (push) -> опрос источников (фолбэк)."""
    mid = None
    mode = "poll"
    poll_note = ""      # V112: честная причина режима опроса в карточке
    try:
        # 1) HLTV-id: сразу, либо фон-сбор и до 5 попыток по 45 сек
        for attempt in range(5):
            mid = sportsdata.find_hltv_match_smart(name1, name2)
            if mid:
                break
            if attempt == 0:
                start_bg_hltv_refresh()
            await safe_edit(status_msg,
                            f"🔴 Лайв-центр: ищу матч на HLTV "
                            f"(попытка {attempt + 1}/5)…")
            await asyncio.sleep(45)
        info = _watch_tasks.get(key)
        if info is not None:
            info["mid"] = mid
        # 2) scorebot: раунды/деньги/закупы пушем
        if mid and config.SCOREBOT_ENABLED:
            st = await scorebot.bridge.subscribe(mid, name1, name2)
            if st is not None:
                mode = "scorebot"
            else:
                # V112: не врём «матча нет на HLTV» — мост просто не поднялся
                poll_note = ("HLTV-мост не поднялся (браузер/Cloudflare) — "
                             "живые раунды недоступны.")
        elif not mid:
            poll_note = ("Матч не найден в списке HLTV (имя не слинковалось) — "
                         "живые раунды недоступны.")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("live-center: %s", e)

    last_text = ""
    last_edit = 0.0
    poll_misses = 0
    started = time.time()
    finished_text: str | None = None
    try:
        while not finished_text:
            # V111fix: авто-стоп, чтобы задача не висела вечно (утечка задач)
            if time.time() - started > config.LIVE_WATCH_MAX_MINUTES * 60:
                finished_text = (f"🔴 Лайв-центр: {name1} vs {name2}\n"
                                 f"⏱ Слежение остановлено "
                                 f"(лимит {config.LIVE_WATCH_MAX_MINUTES} мин).")
                break
            text = ""
            if mode == "scorebot":
                got = await scorebot.bridge.wait_update(mid, timeout=12)
                st = scorebot.bridge.state(mid)
                if st is None:
                    break
                if st.broken or (not st.got_data
                                 and time.time() - started > 150):
                    mode = "poll"
                    poll_note = ("HLTV-мост молчит (соединение/Cloudflare) — "
                                 "переключился на опрос источников.")
                    log.info("live-center %s: scorebot молчит — режим опроса", mid)
                    continue
                if (got or not last_text) and st.got_data:
                    text = scorebot.format_card(st)
                    if st.match_over:
                        finished_text = text + "\n🏁 Матч завершён"
            else:
                await asyncio.sleep(config.LIVE_WATCH_POLL)
                try:
                    live = await sportsdata.get_live()
                except Exception:
                    live = []
                u = next((x for x in live if x.get("uid") == key), None)
                if u:
                    poll_misses = 0
                    text = _poll_card(u, name1, name2, poll_note)
                else:
                    poll_misses += 1
                    if poll_misses >= 2:
                        finished_text = (f"🔴 Лайв-центр: {name1} vs {name2}\n"
                                         "🏁 Матч завершился или данные пропали.")
            if not text and not finished_text:
                continue
            out = finished_text or text
            if (out != last_text
                    and time.time() - last_edit >= config.SCOREBOT_EDIT_GAP):
                try:
                    await safe_edit(status_msg, out, parse_mode="HTML",
                                    reply_markup=_watch_kb(key))
                    last_text = out
                    last_edit = time.time()
                except Exception:
                    pass
        if finished_text and finished_text != last_text:
            try:
                await safe_edit(status_msg, finished_text, parse_mode="HTML",
                                reply_markup=_watch_kb(key))
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    finally:
        if mid:
            try:
                await scorebot.bridge.unsubscribe(mid)
            except Exception:
                pass
        _watch_tasks.pop(key, None)


@dp.callback_query(F.data.startswith("watch:"))
async def cb_watch(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    if key in _watch_tasks:
        await safe_answer(callback, "Уже слежу за этим матчем — карточка в чате",
                          show_alert=True)
        return
    try:
        live = await sportsdata.get_live()
    except Exception:
        live = []
    u = next((x for x in live if x.get("uid") == key), None)
    if not u:
        await safe_answer(callback, "Матч уже не в лайве — обнови список 🔴",
                          show_alert=True)
        return
    await safe_answer(callback)
    teams = u.get("teams") or []
    names = [(t or {}).get("name", "") for t in teams]
    if len(names) != 2 or not all(names):
        parts = (u.get("title") or "").split(" vs ")
        names = parts if len(parts) == 2 else ["?", "?"]
    name1, name2 = names[0], names[1]
    status = await callback.message.answer(
        f"🔴 Запускаю Лайв-центр: <b>{name1} vs {name2}</b>\n"
        "Ищу матч на HLTV: scorebot даёт раунды, деньги и закупы в реальном "
        "времени…", parse_mode="HTML")
    task = asyncio.create_task(_live_watch_task(key, name1, name2, status))
    _watch_tasks[key] = {"task": task, "mid": None}


@dp.callback_query(F.data.startswith("livestop:"))
async def cb_livestop(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    info = _watch_tasks.pop(key, None)
    if info:
        info["task"].cancel()
    await safe_answer(callback, "Слежение остановлено")


@dp.callback_query(F.data.startswith("watchai:"))
async def cb_watch_ai(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    info = _watch_tasks.get(key) or {}
    mid = info.get("mid")
    st = scorebot.bridge.state(mid) if mid else None
    if not st or not st.got_data:
        await safe_answer(callback, "Данных scorebot ещё нет — матч не на HLTV "
                                    "или мост ещё поднимается",
                          show_alert=True)
        return
    if not ai_assist.ai_available():
        await safe_answer(callback, "AI-ключ не задан — /aihelp", show_alert=True)
        return
    await safe_answer(callback)
    s1, s2 = st.scores()
    a1, a2 = st.avgs()
    facts = [f"Матч: {st.name1} vs {st.name2}, карта {st.map_name or '?'}, "
             f"раунд {st.round_no}, счёт {s1}:{s2} "
             f"(первый столбец играет сторону {st.side1})."]
    if a1 is not None:
        facts.append(f"{st.name1}: ≈{a1:.0f}$/игрок, закуп {scorebot.buy_label(a1)}.")
    if a2 is not None:
        facts.append(f"{st.name2}: ≈{a2:.0f}$/игрок, закуп {scorebot.buy_label(a2)}.")
    wname, cnt = st.streak
    if cnt >= 2 and wname is not None:
        facts.append(f"Серия: {st.name1 if wname == 0 else st.name2} — "
                     f"{cnt} раунда подряд.")
    if st.last_rounds:
        hist = []
        for r in list(st.last_rounds)[-8:]:
            w = st.name1 if r["winner_idx"] == 0 else st.name2
            hist.append(f"#{r['n']} {w} ({r['wt'] or '?'})")
        facts.append("Последние раунды: " + ", ".join(hist))
    if st.kills:
        facts.append("Киллы: " + "; ".join(list(st.kills)[-4:]))
    answer = await ai_assist.chat_cached(
        "live:" + st.mid + ":" + str(st.round_no),
        _LIVE_AI_SYSTEM, "\n".join(facts))
    if answer:
        await callback.message.answer(f"🧠 {answer}")
    else:
        await callback.message.answer(
            "⚠️ AI не ответил — попробуй ещё раз через минуту.")


@dp.message(Command("sbdump"))
async def cmd_sbdump(message: Message):
    """Диагностика scorebot: сырые неизвестные события (для адаптации парсера)."""
    if not allowed(message.from_user.id):
        return
    st = scorebot.bridge.latest_state()
    if not st:
        await message.answer("scorebot не активен — активных подписок нет.")
        return
    lines = [f"scorebot {st.mid}: {st.name1} vs {st.name2}",
             f"got_data={st.got_data} broken={st.broken} map={st.map_name} "
             f"ct/t={st.ct_score}:{st.t_score} side1={st.side1} "
             f"money={st.money}", ""]
    for ts, key, raw in list(st.debug)[-15:]:
        lines.append(f"• {key}: {raw[:150]}")
    if not st.debug:
        lines.append("Неизвестных событий нет — парсер разобрал всё.")
    await message.answer("\n".join(lines)[:3800])


# ------------------------------------------- V114: слежение-дайджест (⭐)

def _follow_kb(uid: str) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Не следить",
                                callback_data=f"unfollow:{uid}"))
    return kb.as_markup()


async def _follow_digest(uid: str, name1: str, name2: str):
    """Собрать дайджест матча: scorebot (раунды/деньги/закупы), иначе
    опрос источников (только счёт по картам).
    Возврат: (текст, матч_окончен) либо (None, False), если матча нет."""
    mid = sportsdata.find_hltv_match_smart(name1, name2)
    if mid and config.SCOREBOT_ENABLED:
        st = scorebot.bridge.state(mid)
        if st is None:
            st = await scorebot.bridge.subscribe(mid, name1, name2)
        if st is not None and st.got_data and not st.broken:
            s1, s2 = st.scores()
            buy = ""
            a1, a2 = st.avgs()
            if a1 is not None and a2 is not None:
                buy = (f" · закуп {scorebot.buy_label(a1)}"
                       f" / {scorebot.buy_label(a2)}")
            streak = ""
            wname, cnt = st.streak
            if cnt >= 3 and wname is not None:
                streak = (f" · серия {cnt} р. у "
                          f"{st.name1 if wname == 0 else st.name2}")
            head = "🏁" if st.match_over else "⭐"
            over = "\n🏁 Матч завершён — слежение снято." if st.match_over else ""
            text = (f"{head} <b>{st.name1} {s1}:{s2} {st.name2}</b>\n"
                    f"Карта: {st.map_name or '?'} · раунд {st.round_no}"
                    f"{buy}{streak}{over}")
            return text, st.match_over
    # фолбэк: опрос источников
    try:
        live = await sportsdata.get_live()
    except Exception:
        live = []
    u = next((x for x in live if x.get("uid") == uid), None)
    if u:
        teams = u.get("teams") or []
        names = [(t or {}).get("name") or "" for t in teams]
        n1, n2 = (names + [name1, name2])[:2] if all(names) else (name1, name2)
        sc = u.get("series_score")
        s = f"{sc[0]}:{sc[1]}" if sc else "—:—"
        gt = u.get("games_total")
        mapinfo = f" · карта {u.get('current_game')}/{gt}" if gt else ""
        text = (f"⭐ <b>{n1} {s} {n2}</b>{mapinfo}\n"
                f"<i>Матч вне HLTV-сопоставления — только счёт по картам.</i>")
        return text, False
    return None, False


async def _follow_loop():
    """V114: фоновый цикл по ⭐-матчам: раз в FOLLOW_POLL проверяет лайв
    и раз в FOLLOW_DIGEST_GAP присылает дайджест счёта в чат.
    Матч кончился — финальное сообщение и авто-снятие слежения."""
    log.info("Слежение-дайджест запущено (проверка %s сек, дайджест ~%s сек)",
             config.FOLLOW_POLL, config.FOLLOW_DIGEST_GAP)
    while True:
        try:
            rows = db.get_watchlist()
            for row in rows:
                uid = row["uid"]
                name1, name2 = row["name1"] or "", row["name2"] or ""
                try:
                    text, over = await _follow_digest(uid, name1, name2)
                except Exception as e:
                    log.debug("follow %s: %s", uid, e)
                    continue
                if text is None:
                    misses = (row["misses"] or 0) + 1
                    db.watch_update(uid, row["last_text"] or "", misses=misses)
                    if misses >= 2:
                        final = (f"⭐ {name1} vs {name2}\n"
                                 "🏁 Матч завершился (или данные пропали) "
                                 "— слежение снято.")
                        try:
                            await bot.send_message(row["chat_id"], final,
                                                   parse_mode="HTML")
                        except Exception:
                            pass
                        db.remove_watch(uid)
                    continue
                changed = text != (row["last_text"] or "")
                enough = (time.time() - (row["last_ts"] or 0)
                          >= config.FOLLOW_DIGEST_GAP)
                if changed and enough:
                    try:
                        await bot.send_message(row["chat_id"], text,
                                               parse_mode="HTML",
                                               reply_markup=_follow_kb(uid))
                    except Exception as e:
                        log.debug("follow send %s: %s", uid, e)
                        continue
                    db.watch_update(uid, text)
                elif changed:
                    db.watch_update(uid, row["last_text"] or "", misses=0)
                if over:
                    db.remove_watch(uid)
        except Exception as e:
            log.warning("follow-loop: %s", e)
        await asyncio.sleep(config.FOLLOW_POLL)


@dp.callback_query(F.data.startswith("follow:"))
async def cb_follow(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    uid = callback.data.split(":", 1)[1]
    name1 = name2 = ""
    try:
        live = await sportsdata.get_live()
        u = next((x for x in live if x.get("uid") == uid), None)
        if u:
            teams = u.get("teams") or []
            names = [(t or {}).get("name", "") for t in teams]
            if len(names) == 2 and all(names):
                name1, name2 = names
    except Exception:
        pass
    if not name1:
        await safe_answer(callback, "Матч уже не в лайве — следить не за чем",
                          show_alert=True)
        return
    n = db.add_watch(uid, name1, name2, callback.message.chat.id)
    if n > config.FOLLOW_MAX_PER_CHAT:
        db.remove_watch(uid)
        await safe_answer(
            callback,
            f"Лимит — {config.FOLLOW_MAX_PER_CHAT} матчей. Сними ⭐ с одного "
            "из прошлых, чтобы добавить новый.", show_alert=True)
        return
    await safe_answer(
        callback,
        f"⭐ Слежу за {name1} vs {name2} — счёт будет приходить в чат "
        f"каждые ~{max(1, config.FOLLOW_DIGEST_GAP // 60)} мин.")


@dp.callback_query(F.data.startswith("unfollow:"))
async def cb_unfollow(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    uid = callback.data.split(":", 1)[1]
    db.remove_watch(uid)
    await safe_answer(callback, "Слежение снято ⭐")


# ---------------------------------------------------------------- запуск

async def periodic_prefetch():
    """V106: фоновый цикл каждые 30 минут — к твоему клику данные УЖЕ в базе:
    свежий список матчей, карты+форма всех команд, H2H всех пар, кефы (HTTP-БК)."""
    while True:
        try:
            age = db.matches_age()
            if age is None or age >= 30 * 60:
                try:
                    fast = await collector.collect_matches_via_sources()
                except Exception:
                    fast = None
                if not fast:
                    try:
                        await collector.collect_matches_bo3()
                    except Exception as e:
                        log.info("prefetch: bo3 список: %s", e)
            collector.kick_warmup()
            collector.kick_h2h_warmup()
            roster_watch.kick_roster_watch()   # V109: составы раз в 4 часа
            raw_last = db.get_meta("last_odds_sweep", "")
            try:
                sweep_age = (time.time() - float(raw_last)) if raw_last else 1e9
            except ValueError:
                sweep_age = 1e9
            if sweep_age >= 30 * 60:
                try:
                    await collector.odds_sweep(with_browser=False)
                except Exception as e:
                    log.info("prefetch: кефы: %s", e)
        except Exception as e:
            log.warning("prefetch: %s", e)
        await asyncio.sleep(30 * 60)


async def main():
    db.init_db()
    db.purge_bo3_team_cache()   # V114: разовая зачистка отравленного кэша bo3
    db.migrate_v115_stats_namespaces()   # V115: единый namespace статистики
    # V106: сразу греем данные, чтобы первый клик был мгновенным
    asyncio.create_task(periodic_prefetch())
    asyncio.create_task(_follow_loop())   # V114: дайджесты по ⭐-матчам
    try:
        log.info("Бот запущен, ожидаю сообщения…")
        await dp.start_polling(bot)
    finally:
        await scorebot.bridge.close()     # V111: страница-мост scorebot
        await collector.close_browser()
        await ai_assist.close_session()   # V109


if __name__ == "__main__":
    asyncio.run(main())
