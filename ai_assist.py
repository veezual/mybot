"""
ai_assist.py — V109/V110: AI-аналитик в паре со статистикой бота.

Подключается к ЛЮБОМУ OpenAI-совместимому API по ключу из .env:
  • Groq (рекомендую, бесплатный и самый быстрый):
      AI_API_BASE=https://api.groq.com/openai/v1
      AI_MODEL=openai/gpt-oss-120b
      Ключ: https://console.groq.com/keys
      (старую llama-3.3-70b-versatile Groq снял с сервиса 16.08.2026 —
       теперь при 404 «model_not_found» бот САМ подбирает доступную
       модель из AI_MODEL_FALLBACKS и пишет это в лог)
  • Google Gemini (AI Studio, бесплатный tier):
      AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
      AI_MODEL=gemini-2.5-flash
      Ключ: https://aistudio.google.com/apikey
  • OpenRouter (много бесплатных моделей):
      AI_API_BASE=https://openrouter.ai/api/v1
      Ключ: https://openrouter.ai/keys

Принципы:
  • AI получает ТОЛЬКО факты из нашей базы (карты с числом игр, форма,
    H2H, кефы, смена ростера, вердикт модели) — он ИНТЕРПРЕТИРУЕТ их,
    а не выдумывает;
  • без ключа всё работает по-старому: AI-кнопки честно скажут,
    что ключ не задан (graceful degradation);
  • ответы кэшируются на AI_CACHE_TTL (15 мин) — повторный разбор той же
    пары отвечает мгновенно и не жжёт лимиты;
  • таймаут AI_TIMEOUT — зависший запрос не вешает бота.
"""
import asyncio
import hashlib
import json
import logging
import time

import aiohttp

import config
import database as db
import deepstats
from odds_api import align_odds
from predict import predict_match, BK_LABELS

log = logging.getLogger("ai_assist")

_session: aiohttp.ClientSession | None = None
_cache: dict[str, tuple[float, str]] = {}      # ключ -> (время, ответ)
_model_override: str | None = None             # подобранная при 404 модель
_discovery_done = False                        # автоподбор — раз за запуск


def ai_available() -> bool:
    return bool(config.AI_API_KEY)


# --------------------------------------------------------- автоподбор модели

def _fallback_models() -> list[str]:
    """Модели-приёмники (без основной): порядок = приоритет."""
    seen: list[str] = []
    for m in [config.AI_MODEL] + list(getattr(config, "AI_MODEL_FALLBACKS", [])):
        if m and m not in seen:
            seen.append(m)
    return seen[1:]


async def _discover_model() -> str | None:
    """Провайдеры снимают старые модели с сервиса (Groq убил llama-3.3
    16.08.2026 -> 404 model_not_found). Спрашиваем у API список доступных
    моделей и берём первую живую из AI_MODEL_FALLBACKS. Один раз за запуск."""
    global _model_override, _discovery_done
    _discovery_done = True
    if not ai_available():
        return None
    url = config.AI_API_BASE.rstrip("/") + "/models"
    try:
        sess = await _get_session()
        async with sess.get(
            url,
            headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
        ) as resp:
            if resp.status != 200:
                log.warning("AI: список моделей недоступен (HTTP %s)", resp.status)
                return None
            data = await resp.json()
        ids = {(m or {}).get("id") for m in (data.get("data") or [])}
        if config.AI_MODEL in ids:
            # модель числится доступной — 404 был по другой причине,
            # подмену не делаем
            return None
        for cand in _fallback_models():
            if cand in ids:
                _model_override = cand
                log.warning("AI: модель '%s' недоступна (снята с сервиса?) — "
                            "переключаюсь на '%s'. Пропиши её в .env "
                            "(AI_MODEL=...), чтобы не подбирать каждый раз.",
                            config.AI_MODEL, cand)
                return cand
        log.warning("AI: среди моделей API нет известных нам (%s); "
                    "доступны: %s", ", ".join(_fallback_models()),
                    ", ".join(sorted(i for i in ids if i))[:400])
        return None
    except Exception as e:
        log.warning("AI: автоподбор модели не удался: %s", e)
        return None


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.AI_TIMEOUT),
            headers={"User-Agent": "cs2-analytics-bot/1.09"})
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
    _session = None


async def chat(system: str, user: str, max_tokens: int | None = None) -> str | None:
    """Один запрос к OpenAI-совместимому /chat/completions. None = ошибка."""
    if not ai_available():
        return None
    url = config.AI_API_BASE.rstrip("/") + "/chat/completions"

    def _payload(model: str) -> dict:
        p = {
            "model": model,
            "temperature": config.AI_TEMP,
            "max_tokens": max_tokens or config.AI_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # gpt-oss — «рассуждающая» модель: часть токенов уходит на размышления.
        # Для наших коротких сводок хватит минимума — иначе медленно и
        # ответ можно не уложить в max_tokens.
        if "gpt-oss" in model:
            p["reasoning_effort"] = "low"
        return p

    async def _post(model: str) -> tuple[int, str | None]:
        sess = await _get_session()
        async with sess.post(
            url,
            json=_payload(model),
            headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                log.warning("AI HTTP %s: %s", resp.status, body)
                return resp.status, None
            data = await resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            return 200, (text or "").strip() or None

    try:
        status, answer = await _post(_model_override or config.AI_MODEL)
        # 404 model_not_found / 400 model_decommissioned — модель сняли с
        # сервиса: подбираем живую и повторяем ОДИН раз
        if answer is None and status in (400, 404) and not _discovery_done:
            if await _discover_model():
                _, answer = await _post(_model_override)
        return answer
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("AI запрос: %s", e)
        return None


async def chat_cached(cache_key: str, system: str, user: str,
                      max_tokens: int | None = None) -> str | None:
    """chat() с кэшем по ключу (быстрый повторный отклик + экономия лимитов)."""
    now = time.time()
    # подчистка старых записей
    if len(_cache) > 64:
        for k in [k for k, (ts, _) in _cache.items() if now - ts > config.AI_CACHE_TTL]:
            _cache.pop(k, None)
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < config.AI_CACHE_TTL:
        return hit[1]
    answer = await chat(system, user, max_tokens)
    if answer:
        _cache[cache_key] = (now, answer)
    return answer


# ================================================================ факты

def collect_match_facts(id1: str, name1: str, id2: str, name2: str) -> dict:
    """Всё, что бот знает о паре, — компактным словарём (БД, без сети).

    V113: ПОЛНЫЙ пакет — карты с выборкой, форма с серией, H2H с турнирами,
    составы, рейтинги игроков HLTV (3 мес), мировой ранг, все кефы,
    вердикт модели с ключевыми причинами.
    """
    maps1, _ = db.get_maps_any(id1)
    maps2, _ = db.get_maps_any(id2)
    games1 = db.get_map_games_any(id1)
    games2 = db.get_map_games_any(id2)
    form1 = db.get_form_any(id1)
    form2 = db.get_form_any(id2)
    rows = db.get_h2h_any(id1, id2)

    key = ":".join(sorted([str(id1), str(id2)]))
    raw_odds = db.get_odds(key, 24 * 3600)   # за сутки — кефы ещё показательны
    aligned = align_odds(raw_odds, name1, name2) if raw_odds else {}

    alert1 = db.get_roster_alert_any(id1, config.ROSTER_ALERT_DAYS)
    alert2 = db.get_roster_alert_any(id2, config.ROSTER_ALERT_DAYS)

    # V113: глубокие данные — только кэш (сеть тянет AI-кнопка заранее)
    players1 = deepstats.cached_player_stats(id1)
    players2 = deepstats.cached_player_stats(id2)
    rk1 = deepstats.rank_for(name1)
    rk2 = deepstats.rank_for(name2)

    pred = predict_match(name1, maps1, name2, maps2, rows, odds=aligned,
                         form1=form1, form2=form2,
                         games1=games1, games2=games2,
                         alert1=alert1, alert2=alert2,
                         players1=players1, players2=players2,
                         rank1=rk1, rank2=rk2)

    def _fmt_side(maps, games, form, nm):
        parts = []
        for m, wr in sorted(maps.items(), key=lambda x: -x[1]):
            n = games.get(m)
            parts.append(f"{m} {wr:.0f}%" + (f" (n={n})" if n else ""))
        f = ""
        if form and form.get("total"):
            wr = 100.0 * form["wins"] / form["total"]
            seq = "".join("В" if x else "П" for x in (form.get("last") or [])[:10])
            f = (f"форма: {form['wins']}/{form['total']} ({wr:.0f}%), "
                 f"последние: {seq} "
                 f"(серия {'побед' if form.get('streak_won') else 'поражений'} "
                 f"{form.get('streak', 0)})")
        return f"[{nm}] карты: " + (", ".join(parts) or "нет данных") + (". " + f if f else "")

    facts_lines = [
        f"Матч: {name1} vs {name2}.",
        _fmt_side(maps1, games1, form1, name1),
        _fmt_side(maps2, games2, form2, name2),
    ]
    # V113: мировой ранг HLTV
    if rk1 or rk2:
        facts_lines.append(
            f"Мировой ранг HLTV: {name1} #{rk1}"
            + (" (вне топ-30)" if not rk1 else "")
            + f", {name2} #{rk2}"
            + (" (вне топ-30)" if not rk2 else "") + ".")
    # V113: рейтинги игроков (rating 2.1 за 3 месяца, с выборкой карт)
    for nm, plist in ((name1, players1), (name2, players2)):
        if plist:
            row = ", ".join(
                f"{p['nickname']} {p['rating']:.2f}"
                + (f" (n={p['maps']})" if p.get("maps") else "")
                for p in plist[:5])
            facts_lines.append(f"Игроки {nm} (HLTV rating 2.1, 3 мес): {row}.")
    # V112/V113: составы из кэша БД (монитор ростеров/прогноз)
    for tid, nm in ((str(id1), name1), (str(id2), name2)):
        try:
            r = db.get_roster(tid)
        except Exception:
            r = None
        if r and r.get("players"):
            facts_lines.append(f"Состав {nm}: {', '.join(r['players'][:5])}.")
    if rows:
        facts_lines.append(
            f"Личные встречи: {pred['h2h']['w1']}:{pred['h2h']['w2']} "
            f"(учтено {pred['h2h']['total']}).")
        # V113: последние 5 с датами/счётом/турниром — AI видит контекст серии
        for r in (pred["h2h"].get("last") or [])[:5]:
            if r.get("winner"):
                facts_lines.append(
                    f"  H2H {r.get('date', '')}: {r.get('t1', '')} "
                    f"{r.get('score', '')} {r.get('t2', '')}"
                    + (f" ({r.get('event', '')})" if r.get("event") else ""))
    else:
        facts_lines.append("Личных встреч в базе нет.")
    if aligned:
        ins = pred.get("odds") or {}
        if ins.get("implied1") is not None:
            facts_lines.append(
                f"Рынок (без маржи): {name1} {ins['implied1']:.0f}% / "
                f"{name2} {ins['implied2']:.0f}%.")
            for row in ins.get("rows", []):
                facts_lines.append(f"Кефы {row['label']}: {row['k1']:.2f}/{row['k2']:.2f}.")
    if alert1:
        facts_lines.append(f"ВНИМАНИЕ: у {name1} недавно смена ростера "
                           f"(ушли: {', '.join(alert1.get('removed') or []) or '?'}; "
                           f"пришли: {', '.join(alert1.get('added') or []) or '?'}).")
    if alert2:
        facts_lines.append(f"ВНИМАНИЕ: у {name2} недавно смена ростера "
                           f"(ушли: {', '.join(alert2.get('removed') or []) or '?'}; "
                           f"пришли: {', '.join(alert2.get('added') or []) or '?'}).")

    facts_lines.append(
        f"Вердикт нашей статистической модели: пик {pred.get('pick') or 'нет'}, "
        f"уверенность {pred.get('confidence')}%. "
        f"Сила: {pred.get('power1', 50):.1f} против {pred.get('power2', 50):.1f}.")
    # V113: ключевые причины модели (верхние 4, без HTML) — AI строит
    # обоснование поверх них и спорит с ними, если видит смысл
    import re as _re
    for rs in (pred.get("reasons") or [])[:4]:
        facts_lines.append("Модель: " + _re.sub(r"</?b>", "", rs))

    return {
        "facts": "\n".join(facts_lines),
        "pred": pred,
        "name1": name1, "name2": name2,
        "has_data": bool(maps1 or maps2 or form1 or form2 or rows
                         or players1 or players2),
    }


# ================================================================ промпты

_MATCH_SYSTEM = (
    "Ты — профессиональный betting-аналитик по CS2. Тебе дают ПОЛНЫЙ пакет "
    "проверенных фактов из базы: мировой ранг HLTV, винрейты карт с размером "
    "выборки (n), форма с исходами последних матчей, рейтинг игроков (HLTV "
    "rating 2.1 за 3 месяца, n=карт), личные встречи со счётом и турнирами, "
    "составы, коэффициенты букмекеров и вердикт статистической модели.\n"
    "Правила:\n"
    "1. Используй ТОЛЬКО эти факты. Ничего не выдумывай (новости, мотивация, "
    "трансферы — их нет в данных) и не добавляй знания извне.\n"
    "2. Отвечай по-русски, до 220 слов, строго по структуре:\n"
    "   «Итог» — фаворит, оценка вероятности и уверенность (1-2 фразы);\n"
    "   «Почему» — 2-3 причины с конкретными числами из пакета;\n"
    "   «Карты» — 1-2 ключевые карты: где разрыв и как он влияет на серию;\n"
    "   «Игроки» — кто решает (если рейтинги есть): звезда, средняя топ-5, "
    "перевес по составу;\n"
    "   «Ставки» — Основная: исход + кеф из данных + обоснование одной "
    "строкой; Риск-вариант: исход + кеф, только если видишь перевес, иначе "
    "«нет осмысленного»;\n"
    "   «Не брать» — ловушка: что выглядит заманчиво, но не обосновано;\n"
    "   «Риски» — малые выборки (n), смена ростера, расхождение с рынком.\n"
    "3. Кефы бери только из данных. Если перевеса модели над рынком нет — "
    "прямо скажи: «без перевеса — пропуск или флэт не больше 1%».\n"
    "4. Малые выборки (n<10 по картам, n<15 у игроков) — обязательное "
    "предупреждение в «Рисках».\n"
    "5. Расхождение модели с рынком больше 15 п.п. — обязательный «Риск».\n"
    "6. Никаких гарантий, вероятностный язык («склоняюсь», «чаще»)."
)

_NEWS_SYSTEM = (
    "Ты — редактор новостей киберспорта. Тебе дают заголовки и выдержки "
    "новостей о команде CS2 из поиска. Составь короткую сводку по-русски "
    "(до 120 слов): что произошло (ростер, результаты, дисквалификации, "
    "переходы), и как это может повлиять на ближайшие матчи. Если новости "
    "противоречат друг другу — отметь это. Ничего не додумывай сверх "
    "источников. В конце перечисли источники в одну строку."
)


async def match_analysis(facts: dict) -> str | None:
    """AI-разбор пары по фактам. None — AI недоступен."""
    if not facts.get("has_data"):
        return None
    cache_key = "m:" + hashlib.sha1(
        (facts["facts"] + config.AI_MODEL).encode("utf-8")).hexdigest()[:24]
    return await chat_cached(cache_key, _MATCH_SYSTEM, facts["facts"])


async def news_summary(team_name: str, items: list[dict]) -> str | None:
    """AI-сводка по новостям команды. None — AI недоступен."""
    if not items:
        return None
    lines = []
    for it in items:
        dt = (it.get("date") or "")[:16].replace("T", " ")
        lines.append(f"- [{it.get('source') or '?'}"
                     + (f", {dt}" if dt else "") + "] "
                     + (it.get("title") or ""))
        if it.get("body"):
            lines.append("  " + it["body"][:300])
    user = f"Команда: {team_name} (CS2). Новости:\n" + "\n".join(lines)
    cache_key = "n:" + hashlib.sha1(user.encode("utf-8")).hexdigest()[:24]
    return await chat_cached(cache_key, _NEWS_SYSTEM, user)
