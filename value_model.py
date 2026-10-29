"""
value_model.py — ЭТАП 3: value-модель.

Логика:
  1. Вероятность победы по нашей модели: сила = карты + H2H (predict.py).
  2. Рыночная вероятность из кефов БК без маржи (predict.odds_insights).
  3. Перевес (edge) = модель − рынок. Сигнал: edge >= VALUE_MIN_EDGE и EV > 0.
  4. Размер ставки: флэт 1% банка ИЛИ четверть Келли (безопасная доля).

V107 (защита от сломанной модели):
  • сигнал блокируется, если модель расходится с рынком >= VALUE_MAX_DIVERGENCE
    п.п. — перекос такого размера почти всегда означает ошибку данных
    (неверно сопоставленные команды/карты), а не золото;
  • ставка по сигналу никогда не превышает VALUE_MAX_STAKE_PCT % банка,
    даже если ¼ Келли требует больше.

V108:
  • матчи с неполными данными (pred.low_data — по одной из команд нет ни
    карт, ни формы) не создают сигналов вовсе: половина картины — не
    основание для ставки.

Ключевое свойство: скан работает ТОЛЬКО по кэшу (карты, H2H, кефы из SQLite).
Он не открывает ни браузер, ни HLTV — поэтому выполняется за секунды.
Свежие кефы перед сканом собирает collector.odds_sweep() (bot.py делает это сам).
"""
import logging

import config  # V111fix: нужен для config.ROSTER_ALERT_DAYS ниже (был NameError)
import database as db
import deepstats
from config import (VALUE_MIN_EDGE, VALUE_ODDS_TTL,
                    VALUE_MAX_DIVERGENCE, VALUE_MAX_STAKE_PCT,
                    VALUE_MIN_EDGE_PARTIAL)
from predict import predict_match, BK_LABELS
from odds_api import align_odds

log = logging.getLogger("value")

DISCLAIMER = ("⚠️ Аналитика, а не финансовый совет. Ставки — всегда риск: "
              "играй только на деньги, которые готов потерять.")


def kelly_full(p: float, odd: float) -> float:
    """Доля банка по критерию Келли (0..1). p — вероятность (0..1)."""
    if odd <= 1.0 or p <= 0.0:
        return 0.0
    f = (p * odd - 1.0) / (odd - 1.0)
    return max(0.0, min(f, 1.0))


def _side_signal(team, odd, model_p, market_p, min_edge: float | None = None):
    """Оценка одной стороны: сигнал или нет. Возврат: dict | None.
    V115: min_edge — порог эджа (повышенный для матчей с неполными данными)."""
    edge = model_p - market_p
    if edge < (min_edge if min_edge is not None else VALUE_MIN_EDGE):
        return None
    p = model_p / 100.0
    ev = p * odd - 1.0            # матожидание на 1 ед. ставки
    if ev <= 0.0:
        return None               # перевес есть, а кеф уже «съеден» маржой
    kf = kelly_full(p, odd)
    kq = kf / 4.0 * 100.0         # ¼ Келли, % банка
    # V107: даже если Келли требует больше — ставка не выше VALUE_MAX_STAKE_PCT
    kq_capped = min(kq, VALUE_MAX_STAKE_PCT)
    return {
        "team": team, "odd": odd,
        "model_p": model_p, "market_p": market_p,
        "edge": edge, "ev": ev * 100.0,
        "kelly_quarter": kq_capped,
        "stake_capped": kq > VALUE_MAX_STAKE_PCT,
    }


def _data_partial(id1: str, id2: str) -> bool:
    """V115: данных по матчу мало?
    Форма есть не у обеих команд либо суммарная выборка карт у одной из
    сторон меньше 10 игр. На таких матчах модель гуляет сильнее — порог
    эджа повышаем (VALUE_MIN_EDGE_PARTIAL), а сигнал помечаем."""
    f1 = db.get_form_any(id1)
    f2 = db.get_form_any(id2)
    if not f1 or not f2:
        return True
    g1 = sum((db.get_map_games_any(id1) or {}).values())
    g2 = sum((db.get_map_games_any(id2) or {}).values())
    return min(g1, g2) < 10


def scan_matches(matches: list[dict]) -> tuple[list[dict], list[tuple]]:
    """
    Скан всех матчей по кэшу. Ничего не собирает.
    Возврат: (signals, skipped)
      signals: [{team1, team2, match_key, ...signal, bookmaker, start_time}]
      skipped: [(name1 vs name2, причина)]
    """
    signals: list[dict] = []
    skipped: list[tuple] = []

    for m in matches:
        id1, id2 = str(m["team1_id"]), str(m["team2_id"])
        name1, name2 = m["team1_name"], m["team2_name"]
        match_key = ":".join(sorted([id1, id2]))

        maps1 = db.get_maps_any(id1)[0]
        maps2 = db.get_maps_any(id2)[0]
        if not maps1 or not maps2:
            skipped.append((f"{name1} vs {name2}",
                            "нет карт в кэше — открой прогноз матча"))
            continue

        h2h_rows = db.get_h2h_any(id1, id2)
        raw_odds = db.get_odds(match_key, VALUE_ODDS_TTL)
        # align_odds: {bk: (k1, k2)} с учётом сохранённых сторон (кто П1/П2)
        odds = align_odds(raw_odds, name1, name2) if raw_odds else {}
        if not odds:
            skipped.append((f"{name1} vs {name2}", "нет свежих кефов"))
            continue
        try:
            # V113: глубокие данные — только из кэша (скан быстрый, без сети);
            # их туда кладёт фоновый прогрев или AI-разбор
            pred = predict_match(name1, maps1, name2, maps2, h2h_rows, odds=odds,
                                 form1=db.get_form_any(id1), form2=db.get_form_any(id2),
                                 games1=db.get_map_games_any(id1),
                                 games2=db.get_map_games_any(id2),
                                 alert1=db.get_roster_alert_any(id1, config.ROSTER_ALERT_DAYS),
                                 alert2=db.get_roster_alert_any(id2, config.ROSTER_ALERT_DAYS),
                                 players1=deepstats.cached_player_stats(id1),
                                 players2=deepstats.cached_player_stats(id2),
                                 rank1=deepstats.rank_for(name1),
                                 rank2=deepstats.rank_for(name2))
        except Exception as e:
            log.warning("predict %s: %s", match_key, e)
            skipped.append((f"{name1} vs {name2}", "ошибка модели"))
            continue

        # V109: свежая смена ростера — статистика врёт, сигналов не даём
        if pred.get("roster_alert"):
            skipped.append((f"{name1} vs {name2}",
                            "свежая смена ростера — статистика ненадёжна, "
                            "сигнал заблокирован"))
            continue

        # V108: неполные данные — сигнала не будет
        if pred.get("low_data"):
            skipped.append((f"{name1} vs {name2}",
                            "мало данных по командам — сигнал заблокирован"))
            continue

        ins = pred["odds"]
        if not ins.get("best1") or not ins.get("best2"):
            skipped.append((f"{name1} vs {name2}", "кефы не разобрались"))
            continue

        model1 = ins["model1"]
        implied1 = ins["implied1"]
        # V115: матчи с неполными данными признаются только при повышенном эдже
        partial = _data_partial(id1, id2)
        min_edge = VALUE_MIN_EDGE_PARTIAL if partial else VALUE_MIN_EDGE
        variants = [
            (name1, ins["best1"][0], ins["best1"][1], model1, implied1),
            (name2, ins["best2"][0], ins["best2"][1],
             100.0 - model1, 100.0 - implied1),
        ]
        for team, bk, odd, model_p, market_p in variants:
            # V107: защита от самоуверенности модели — сигнал с перекосом
            # против рынка >= VALUE_MAX_DIVERGENCE п.п. не создаётся вовсе.
            if abs(model_p - market_p) >= VALUE_MAX_DIVERGENCE:
                skipped.append(
                    (f"{name1} vs {name2}",
                     f"{team}: модель {model_p:.0f}% vs рынок {market_p:.0f}% — "
                     f"расхождение {model_p - market_p:+.0f} п.п., сигнал "
                     f"заблокирован (вероятна ошибка данных)"))
                continue
            sig = _side_signal(team, odd, model_p, market_p, min_edge=min_edge)
            if not sig:
                continue
            sig["partial_data"] = partial
            sig_id = db.save_signal(
                match_key=match_key, team1=name1, team2=name2, pick=team,
                bookmaker=bk, odd=odd, model_p=model_p, market_p=market_p,
                edge=sig["edge"], ev=sig["ev"], kelly=sig["kelly_quarter"])
            signals.append({
                "id": sig_id, "match_key": match_key,
                "team1": name1, "team2": name2, "start_time": m.get("start_time", ""),
                **sig, "bookmaker": bk, "bookmaker_label": BK_LABELS.get(bk, bk),
                "duplicate": sig_id is None,
            })

    signals.sort(key=lambda s: s["edge"], reverse=True)
    return signals, skipped


def format_signal(s: dict) -> str:
    """Одна строка-сигнал для сообщения в Telegram (HTML)."""
    start = f" · {s['start_time']}" if s.get("start_time") else ""
    dup = " (уже в истории)" if s.get("duplicate") else ""
    # V115: честная метка неполных данных
    part = "\n⚠️ Данные неполные — порог эджа повышен, ставь осторожнее." \
        if s.get("partial_data") else ""
    return (
        f"💎 <b>{s['team1']} vs {s['team2']}</b>{start}\n"
        f"Ставка: <b>{s['team']}</b> · кеф <b>{s['odd']:.2f}</b> "
        f"({s['bookmaker_label']}){dup}\n"
        f"Модель {s['model_p']:.0f}% vs рынок {s['market_p']:.0f}% "
        f"→ edge <b>+{s['edge']:.0f} п.п.</b> · EV <b>+{s['ev']:.1f}%</b>\n"
        f"Банк: флэт 1% или Келли ¼ = <b>{s['kelly_quarter']:.1f}%</b>"
        + (" (потолок защиты V107)" if s.get("stake_capped") else "")
        + part
    )


def format_scan_result(signals: list[dict], skipped: list[tuple]) -> str:
    """Полный текст ответа на «💎 VALUE»."""
    if not signals:
        parts = ["💎 <b>VALUE-скан</b> — перевесов не найдено.",
                 "",
                 "Либо рынок согласен с моделью, либо не хватает данных "
                 "(карты/кефы собираются при клике на матч)."]
        if skipped:
            parts.append("")
            parts.append("Пропущены (нет данных в кэше):")
            parts += [f"• {name} — {why}" for name, why in skipped[:8]]
        parts.append("")
        parts.append(DISCLAIMER)
        return "\n".join(parts)

    parts = [f"💎 <b>VALUE-скан</b> — сигналов: {len(signals)}", ""]
    parts += [format_signal(s) for s in signals[:8]]
    if len(signals) > 8:
        parts.append(f"…и ещё {len(signals) - 8} (показаны лучшие)")
    if skipped:
        parts.append("")
        parts.append(f"Пропущено матчей без данных: {len(skipped)} "
                     "(открой их прогноз, чтобы наполнить кэш).")
    parts.append("")
    parts.append(DISCLAIMER)
    return "\n".join(parts)


STATUS_ICONS = {"pending": "⏳", "win": "✅", "lose": "❌"}


def format_history(rows: list[dict], stats: dict) -> str:
    """Текст «📜 История»: последние сигналы + итоги."""
    if not rows:
        return ("📜 История сигналов пуста.\n\n"
                "Нажми 💎 VALUE — модель просканирует матчи дня и сохранит перевесы.")
    lines = ["📜 <b>История сигналов</b>", ""]
    for r in rows:
        icon = STATUS_ICONS.get(r["status"], "⏳")
        bk = BK_LABELS.get(r["bookmaker"], r["bookmaker"])
        lines.append(
            f"{icon} #{r['id']} <b>{r['pick']}</b> @ {r['odd']:.2f} ({bk}) — "
            f"{r['team1']} vs {r['team2']}, edge +{r['edge']:.0f} п.п."
        )
    lines.append("")
    if stats["n_closed"]:
        lines.append(
            f"Итоги: ✅{stats['n_win']} / ❌{stats['n_lose']} · "
            f"винрейт {stats['hitrate']:.0f}% · ROI {stats['roi']:+.1f}% "
            "(флэт 1 ед.)"
        )
    else:
        lines.append("Закрытых сигналов пока нет — отмечай результаты кнопками ✅/❌.")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
