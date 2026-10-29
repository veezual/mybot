"""
predict.py — аналитика и объяснение прогноза.

Этап 1 (V106, жадная модель): взвешенная сила из 4 компонентов —
        средний винрейт карт, потолок топ-3 карт, ФОРМА (последние 10
        матчей), H2H-доминирование. Отсутствующие компоненты выпадают,
        веса перенормируются. Уверенность — логистическая от разрыва сил.
Этап 2: сравнение с рынком — имплайд-вероятности из кефов БК и перевес (edge)
        нашей модели против линии. Точки расширения для value-модели.

V107 (защита от самоуверенности):
  • в расчёт силы идут ТОЛЬКО карты активного пула (config.ACTIVE_DUTY_MAPS) —
    винрейты Overpass/Train вне пула не должны качать прогноз;
  • санити-чек против рынка: если модель расходится с линией БК сильнее
    PREDICT_MAX_DIVERGENCE п.п., уверенность урезается до
    PREDICT_CONF_CAP_DIVERGENCE, а в причины пишется предупреждение —
    перекос модели против рынка такого размера почти всегда означает,
    что статистика карт не видит главного (ростер, календарь, мотивация);
  • статусы пика против рынка стали симметричными: "value" / "caution" /
    "agree" / "divergence" (раньше любой пик андердога праздновался как VALUE);
  • исправлен баг «Риск: слабейшая карта» — считалась слабейшая карта
    team1, даже если пик был на team2.

V108 (честность при нехватке данных):
  • если по ОБЕИМ командам нет ни карт, ни формы (а H2H пуст) — прогноз
    честно признаётся несостоявшимся: «Недостаточно данных», а не
    «Без явного фаворита» (раньше карточка Leo vs MAYBE выглядела как
    настоящий прогноз 50/50, хотя это просто пустота в базе);
  • если данные есть только по ОДНОЙ стороне — уверенность урезается до
    PREDICT_CONF_CAP_LOWDATA (58%): модель видит половину картины и не
    должна выдавать 80-90%;
  • H2H-одиночка больше не качает уверенность до 87%: без карт и формы
    обеих сторон прогноз считается малоданным и ограничивается тем же
    потолком.

V113 (глубокие данные HLTV — «обоснованные ставки»):
  • в силу добавлены ДВА компонента:
      игроки (0.20) — средний rating 2.1 топ-5 игроков за 3 месяца
        (HLTV /stats/players, только игроки с >= PLAYER_MIN_MAPS карт);
      мировой ранг (0.10) — позиция в HLTV-топе (1-е место = 100, 60+ = 0);
  • веса перебалансированы: карты-среднее 0.30, топ-3 0.15, форма 0.25,
    H2H 0.10, игроки 0.20, ранг 0.10 — компонент без данных просто
    выпадает и веса перенормируются (как раньше);
  • в карточку идут строки: «Мировой ранг HLTV: …» и «Игроки: лучшая
    звезда …, средняя топ-5 …».
"""
import math
import re
import time

import config

# подписи БК для карточек и текста
BK_LABELS = {"fonbet": "Фонбет", "winline": "Винлайн", "pari": "Пари",
             "betboom": "Бетбум"}


def _score_parse(score: str) -> tuple[int, int] | None:
    """'13 - 9' / '2:1' / '16-10' → (13, 9)."""
    m = re.search(r"(\d+)\s*[-–—:]\s*(\d+)", score or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _name_eq(a: str, b: str) -> bool:
    """Нечувствительное к регистру сравнение с подстрокой (NAVI ~ Natus Vincere нет, но NAVI ~ NAVI да)."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def h2h_summary(rows: list[dict], name1: str, name2: str) -> dict:
    """Счёт серии, серии побед, последние матчи."""
    w1 = w2 = 0
    parsed = []
    for r in rows:
        sc = _score_parse(r.get("score", ""))
        if not sc:
            continue
        left, right = sc
        t1, t2 = r.get("t1", ""), r.get("t2", "")
        # определяем, кто из запрошенных команд слева в этой строке
        if _name_eq(t1, name1):
            winner = name1 if left > right else (name2 if right > left else None)
        elif _name_eq(t1, name2):
            winner = name2 if left > right else (name1 if right > left else None)
        else:
            continue
        if winner == name1:
            w1 += 1
        elif winner == name2:
            w2 += 1
        parsed.append({**r, "winner": winner})

    # строки в ответе HLTV идут от свежих к старым
    streak_name, streak_n = None, 0
    for p in parsed:
        if p["winner"] is None:
            break
        if streak_name is None:
            streak_name = p["winner"]
            streak_n = 1
        elif p["winner"] == streak_name:
            streak_n += 1
        else:
            break

    return {
        "w1": w1, "w2": w2,
        "streak_name": streak_name, "streak": streak_n if streak_name else 0,
        "last": parsed[:10],
        "total": w1 + w2,
    }


def odds_insights(name1: str, name2: str, power1: float, power2: float,
                  odds: dict | None) -> dict:
    """
    odds: {bookmaker: (k1 — на name1, k2 — на name2)}.
    Возврат: лучшие кефы, имплайд-вероятности, edge модели против рынка,
    строки для карточки [{label, k1, k2, best}].
    """
    if not odds:
        return {"rows": [], "best1": None, "best2": None,
                "implied1": None, "edge1": None}

    rows, best1 = [], (None, None)   # (bk, k)
    best2 = (None, None)
    for bk, (k1, k2) in odds.items():
        try:
            k1, k2 = float(k1), float(k2)
        except (TypeError, ValueError):
            continue
        if k1 < 1.005 or k2 < 1.005:
            continue
        label = BK_LABELS.get(bk, bk.capitalize())
        best = 1 if k1 > k2 else (2 if k2 > k1 else None)
        rows.append({"label": label, "k1": k1, "k2": k2, "best": best})
        if best1[1] is None or k1 > best1[1]:
            best1 = (bk, k1)
        if best2[1] is None or k2 > best2[1]:
            best2 = (bk, k2)

    if not rows:
        return {"rows": [], "best1": None, "best2": None,
                "implied1": None, "edge1": None}

    k1, k2 = best1[1], best2[1]
    implied1 = (1.0 / k1) / (1.0 / k1 + 1.0 / k2) * 100.0
    total_power = power1 + power2 or 1.0
    model1 = power1 / total_power * 100.0
    edge1 = model1 - implied1  # в п.п.

    return {"rows": rows, "best1": best1, "best2": best2,
            "implied1": implied1, "implied2": 100 - implied1,
            "model1": model1, "edge1": edge1}


def _map_norm(m: str) -> str:
    """"Dust2" / "Dust 2" / "de_dust2" -> "dust2" (для сверки с пулом)."""
    s = (m or "").strip().lower()
    if s.startswith("de_"):
        s = s[3:]
    return s.replace(" ", "").replace("_", "")


_ACTIVE_MAPS = {_map_norm(m) for m in config.ACTIVE_DUTY_MAPS}


def filter_active_maps(maps: dict) -> dict:
    """V107: оставляем только карты активного пула.

    Винрейты вне пула (Overpass, Train, Vertigo и пр.) не участвуют в оценке
    силы: на банне в текущем сезоне их взять нельзя. Защита: если фильтр
    съел ВСЁ (кривые имена/новый пул) — возвращаем вход как есть, чтобы
    модель не осталась без данных.
    """
    if not maps:
        return {}
    out = {m: v for m, v in maps.items() if _map_norm(m) in _ACTIVE_MAPS}
    return out if out else dict(maps)


def shrink_maps(maps: dict, games: dict | None = None) -> tuple[dict, bool]:
    """V109: байесовский шринкейдж винрейтов к 50% по числу игр в выборке.

    "Cache 100%" после 2 игр -> (100*2 + 50*3)/5 = 70%; карта на 20 игр
    почти не меняется. Возврат: (словарь, был_ли_заметный_шринкейдж).
    games пришёл из базы (bo3.gg пишет число карт в выборке) — если данных
    о числе игр нет, винрейты не трогаем.
    """
    if not maps or not games:
        return maps, False
    k = float(config.MAP_SHRINK_PRIOR)
    out, noticeable = {}, False
    for m, wr in maps.items():
        try:
            n = int(games.get(m, 0) or 0)
        except (TypeError, ValueError, AttributeError):
            n = 0
        if n > 0:
            adj = (wr * n + 50.0 * k) / (n + k)
            out[m] = round(adj, 1)
            if abs(adj - wr) >= 5.0:
                noticeable = True
        else:
            out[m] = wr
    return out, noticeable


def _alert_reason(alert: dict | None, team_name: str) -> str | None:
    """V109: текст причины «смена ростера» для карточки (или None)."""
    if not alert:
        return None
    added = ", ".join(alert.get("added") or [])
    removed = ", ".join(alert.get("removed") or [])
    days = max(0, int((time.time() - (alert.get("created_at") or 0)) / 86400))
    when = "сегодня" if days == 0 else f"{days} дн. назад"
    parts = []
    if removed:
        parts.append(f"ушли: {removed}")
    if added:
        parts.append(f"пришли: {added}")
    detail = (" (" + "; ".join(parts) + ")") if parts else ""
    return (f"⚠️ СМЕНА РОСТЕРА: у <b>{team_name}</b> {when} менялся состав"
            f"{detail}. Статистика карт и формы набрана в основном старым "
            f"составом — доверять ей меньше.")


def _maps_as_dict(m) -> dict[str, float]:
    """
    Защитная нормализация (V105): карты могут прийти словарём или списком пар
    [('Ancient', 70.7), ...] — приводим всё к {карта: винрейт}.
    """
    if isinstance(m, dict):
        out = {}
        for k, v in m.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(m, (list, tuple)):
        out = {}
        for item in m:
            try:
                k, v = item[0], item[1]
                out[str(k)] = float(v)
            except (TypeError, ValueError, IndexError):
                continue
        return out
    return {}


def _rating_to_power(r: float) -> float:
    """V113: rating 2.1 -> шкала 0-100. 0.95 → 0, 1.05 → 25, 1.15 → 50,
    1.30 → 88, 1.35+ → 100 (потолок)."""
    return max(0.0, min(100.0, (r - 0.95) / 0.40 * 100.0))


def _rank_to_power(rank: int) -> float:
    """V113: место в HLTV-топе -> 0-100. №1 = 100, №30 ≈ 49, №60+ = 0."""
    return max(0.0, min(100.0, (60.0 - min(int(rank), 60)) / 59.0 * 100.0))


def _players_power(players: list[dict] | None) -> tuple[float, dict] | None:
    """V113: сила по игрокам: средняя топ-5 (с достаточной выборкой).

    Фильтр по PLAYER_MIN_MAPS живёт ЗДЕСЬ (а не только у источника данных):
    любой caller может передать сырые строки из базы — шум с 2-3 картами
    не должен качать силу. Возврат: (0-100, {best, avg5}) либо None.
    """
    rows = []
    for p in players or []:
        try:
            rating = p.get("rating")
            maps = int(p.get("maps") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if rating is None or maps < config.PLAYER_MIN_MAPS:
            continue
        rows.append({"nickname": p.get("nickname") or "?", "rating": float(rating)})
    if len(rows) < 3:
        return None        # 1-2 игрока — не команда, фактор ненадёжен
    top = sorted(rows, key=lambda x: -x["rating"])[:5]
    avg = sum(p["rating"] for p in top) / len(top)
    best = (top[0]["nickname"], top[0]["rating"])
    return _rating_to_power(avg), {"best": best, "avg5": round(avg, 2)}


def _power_components(maps1: dict, form1: dict | None,
                      h2h_share1: float | None,
                      players1: list[dict] | None = None,
                      rank1: int | None = None) -> float:
    """Жадная сила 0-100 из доступных компонентов (веса перенормируются).
      V113: карты-среднее 0.30 · топ-3 потолок 0.15 · форма 0.25 ·
            H2H 0.10 · игроки (HLTV rating) 0.20 · мировой ранг 0.10"""
    comps: list[tuple[float, float]] = []
    if maps1:
        avg = sum(maps1.values()) / len(maps1)
        comps.append((0.30, avg))
        top3 = sorted(maps1.values(), reverse=True)[:3]
        if top3:
            comps.append((0.15, sum(top3) / len(top3)))
    if form1 and form1.get("total"):
        comps.append((0.25, 100.0 * form1["wins"] / form1["total"]))
    if h2h_share1 is not None:
        comps.append((0.10, h2h_share1))
    pp = _players_power(players1)
    if pp is not None:
        comps.append((0.20, pp[0]))
    if rank1 is not None:
        comps.append((0.10, _rank_to_power(rank1)))
    if not comps:
        return 50.0
    wsum = sum(w for w, _ in comps)
    return sum(w * v for w, v in comps) / wsum


def predict_match(name1: str, maps1, name2: str, maps2,
                  h2h_rows: list[dict],
                  odds: dict | None = None,
                  form1: dict | None = None,
                  form2: dict | None = None,
                  games1: dict | None = None,
                  games2: dict | None = None,
                  alert1: dict | None = None,
                  alert2: dict | None = None,
                  players1: list[dict] | None = None,
                  players2: list[dict] | None = None,
                  rank1: int | None = None,
                  rank2: int | None = None) -> dict:
    """Жадная модель (V106): сила = карты + форма + H2H.
    V113: + рейтинг игроков (HLTV rating 2.1, топ-5) и мировой ранг HLTV.
    Чего нет — выпадает, веса перенормируются.
    Уверенность = 50 + 46*(1 - e^(-разрыв/9)) — растёт быстро, потолок 95%.
    V107: карты вне активного пула не учитываются; при расхождении пика
    с рынком >= PREDICT_MAX_DIVERGENCE уверенность урезается.
    V109: games1/games2 — байесовский шринкейдж малых выборок к 50%;
    alert1/alert2 — свежая смена ростера (потолок уверенности + причина).
    players1/2 — [{nickname, rating, maps}] (только rating и maps реально
    нужны); rank1/2 — места в мировом топе HLTV (None = вне топ-30/нет данных).
    Возврат: pick, confidence, reasons[], stats для карточки.
    """
    maps1 = filter_active_maps(_maps_as_dict(maps1))
    maps2 = filter_active_maps(_maps_as_dict(maps2))
    maps1, shrunk1 = shrink_maps(maps1, games1)
    maps2, shrunk2 = shrink_maps(maps2, games2)
    h2h = h2h_summary(h2h_rows, name1, name2)

    # ---- V108: есть ли у команды ХОТЬ что-то (карты или форма) ----
    def _has_side(maps, form) -> bool:
        return bool(maps) or bool(form and form.get("total"))

    has1 = _has_side(maps1, form1)
    has2 = _has_side(maps2, form2)
    low_data = not (has1 and has2)   # хоть одна сторона — пустая

    avg1 = sum(maps1.values()) / len(maps1) if maps1 else 0.0
    avg2 = sum(maps2.values()) / len(maps2) if maps2 else 0.0

    # лучшая карта каждой команды и общий ландшафт
    best1 = max(maps1.items(), key=lambda x: x[1]) if maps1 else ("—", 0.0)
    best2 = max(maps2.items(), key=lambda x: x[1]) if maps2 else ("—", 0.0)

    # карта максимального преимущества фаворита
    common = set(maps1) & set(maps2)
    edge_map, edge_diff, edge_leader = ("—", 0.0, None)
    for m in common:
        d = maps1[m] - maps2[m]
        if abs(d) > abs(edge_diff):
            edge_map, edge_diff = m, d
            edge_leader = name1 if d > 0 else name2

    h2h_share1 = h2h_share2 = None
    if h2h["total"]:
        raw1 = h2h["w1"] / h2h["total"] * 100
        # V106: доверие к малым выборкам — 1 матч не должен качать силу на 15 п.п.
        k = min(1.0, h2h["total"] / 5.0)
        h2h_share1 = 50.0 + (raw1 - 50.0) * k
        h2h_share2 = 100.0 - h2h_share1

    power1 = _power_components(maps1, form1, h2h_share1, players1, rank1)
    power2 = _power_components(maps2, form2, h2h_share2, players2, rank2)

    gap = abs(power1 - power2)
    # V108: совсем пусто (нет карт, формы и даже H2H) — прогноза нет.
    # Раньше это выдавалось как «Без явного фаворита» — как будто модель
    # что-то оценила, хотя данных ноль.
    if not has1 and not has2 and not h2h["total"]:
        pick, confidence = None, 50
        pick_word = "Недостаточно данных"
    elif gap < 2:
        pick, confidence = None, 50
        pick_word = "Без явного фаворита"
    else:
        pick = name1 if power1 > power2 else name2
        confidence = max(52, min(95, round(50 + 46 * (1 - math.exp(-gap / 9)))))
        pick_word = pick

    # V108: однобокие данные не дают права на высокую уверенность
    if low_data and confidence > config.PREDICT_CONF_CAP_LOWDATA:
        confidence = int(config.PREDICT_CONF_CAP_LOWDATA)

    # ---------------- объяснения ----------------
    reasons = []

    # ---- V109: смена ростера — первой строкой, с потолком уверенности ----
    ar1 = _alert_reason(alert1, name1)
    ar2 = _alert_reason(alert2, name2)
    if ar1:
        reasons.append(ar1)
    if ar2:
        reasons.append(ar2)
    if (alert1 or alert2) and confidence > config.ROSTER_CONF_CAP:
        confidence = int(config.ROSTER_CONF_CAP)

    # ---- V108: честная метка нехватки данных — первой строкой ----
    if not has1 and not has2:
        reasons.append(
            "Нет статистики ни по одной из команд (истории матчей не нашлось "
            "ни на bo3.gg, ни на HLTV) — прогноз строить не на чем."
        )
    elif low_data:
        blind = name1 if not has1 else name2
        reasons.append(
            f"По <b>{blind}</b> нет ни карт, ни формы — модель видит только "
            f"одну сторону, уверенность ограничена "
            f"{config.PREDICT_CONF_CAP_LOWDATA}%."
        )

    # ---- V113: мировой ранг HLTV ----
    if rank1 is not None or rank2 is not None:
        r1s = f"#{rank1}" if rank1 else "вне топ-30"
        r2s = f"#{rank2}" if rank2 else "вне топ-30"
        if rank1 and rank2 and rank1 != rank2:
            hi = name1 if rank1 < rank2 else name2
            reasons.append(
                f"Мировой ранг HLTV: <b>{name1}</b> {r1s}, "
                f"<b>{name2}</b> {r2s} — выше {hi}.")
        else:
            reasons.append(
                f"Мировой ранг HLTV: <b>{name1}</b> {r1s}, "
                f"<b>{name2}</b> {r2s}.")

    # ---- V113: рейтинги игроков (HLTV rating 2.1, 3 месяца) ----
    ps1 = _players_power(players1)
    ps2 = _players_power(players2)

    def _pstr(ps) -> str:
        if not ps:
            return "нет данных"
        nick, rat = ps[1]["best"]
        return f"звезда {nick} {rat:.2f}, средняя топ-5 {ps[1]['avg5']:.2f}"

    if ps1 or ps2:
        reasons.append(
            f"Игроки (HLTV, 3 мес): {name1} — {_pstr(ps1)}; "
            f"{name2} — {_pstr(ps2)}.")

    fav, dog = (name1, name2) if avg1 >= avg2 else (name2, name1)
    fav_avg, dog_avg = max(avg1, avg2), min(avg1, avg2)
    if fav_avg:
        reasons.append(
            f"Средний винрейт по картам: <b>{fav}</b> — {fav_avg:.1f}%, "
            f"<b>{dog}</b> — {dog_avg:.1f}%. Разрыв {fav_avg - dog_avg:+.1f} п.п."
        )

    if edge_leader and abs(edge_diff) >= 5:
        lead_wr = maps1[edge_map] if edge_leader == name1 else maps2[edge_map]
        lose_wr = maps2[edge_map] if edge_leader == name1 else maps1[edge_map]
        reasons.append(
            f"Ключевая карта — <b>{edge_map}</b>: {edge_leader} выигрывает "
            f"{lead_wr:.1f}% против {lose_wr:.1f}% у соперника."
        )

    if h2h["total"]:
        leader_h2h = name1 if h2h["w1"] > h2h["w2"] else name2
        reasons.append(
            f"Личные встречи: <b>{h2h['w1']}:{h2h['w2']}</b> "
            f"в пользу {leader_h2h} (учтено {h2h['total']} матчей)."
        )
        if h2h["streak"] >= 2 and h2h["streak_name"]:
            reasons.append(
                f"Серия: {h2h['streak_name']} взял(а) последние {h2h['streak']} встречи подряд."
            )
    else:
        reasons.append("Личных встреч между командами не найдено — фактор не учитывается.")

    # ---- V106: форма (последние матчи) ----
    f1v = form1 if (form1 and form1.get("total")) else None
    f2v = form2 if (form2 and form2.get("total")) else None

    def _frm(f: dict | None) -> str:
        if not f or not f.get("total"):
            return "нет данных"
        wr = 100.0 * f["wins"] / f["total"]
        return f"{f['wins']} из {f['total']} ({wr:.0f}%)"

    if f1v or f2v:
        reasons.append(
            f"Форма: <b>{name1}</b> — {_frm(f1v)}, <b>{name2}</b> — {_frm(f2v)}."
        )
        for nm, fv in ((name1, f1v), (name2, f2v)):
            if fv and fv.get("streak", 0) >= 2:
                if fv.get("streak_won"):
                    reasons.append(f"Момент: {nm} выиграл(а) {fv['streak']} матча подряд.")
                else:
                    reasons.append(f"Риск: {nm} проиграл(а) {fv['streak']} матча подряд.")

    # ---- V112: ОДИН честный блок по картам (было два противоречащих:
    # «Ключевая карта — Dust2: 72% против 36%» и тут же «Dust2 — 66/34» —
    # вторая цифра была ДОЛЕЙ ОТ СУММЫ, а не винрейтом — выглядело как баг)
    if common:
        parts = ", ".join(
            f"{m} {maps1[m]:.0f}/{maps2[m]:.0f}%"
            for m in sorted(common, key=lambda x: -abs(maps1[x] - maps2[x]))[:4]
        )
        reasons.append(
            f"Винрейты на общих картах ({name1}/{name2}): {parts}."
        )
    elif maps1 or maps2:
        reasons.append(
            "Общих карт в активном пуле нет — по картам команды не сравнить."
        )

    # V107: слабейшая карта считается у ТОГО, на кого пик (раньше всегда у team1)
    focus_maps = maps1 if (pick == name1 or not pick) else maps2
    worst = min(focus_maps.items(), key=lambda x: x[1]) if focus_maps else ("—", 0.0)
    if pick and worst[1] < 50:
        reasons.append(
            f"Риск: слабейшая карта у {pick} — <b>{worst[0]}</b> "
            f"({worst[1]:.0f}%), её могут забанить."
        )

    if len(maps1) < 3 or len(maps2) < 3:
        reasons.append("Мало матчей по картам — оценка приблизительная.")

    if shrunk1 or shrunk2:
        reasons.append(
            "Винрейты по 1-2 матчам сглажены к 50% — случайная серия "
            "не должна выглядеть как 100%."
        )

    # ---------------- рынок: кефы БК (Этап 2) ----------------
    ins = odds_insights(name1, name2, power1, power2, odds)
    if ins["rows"]:
        b1bk, b1k = ins["best1"]
        b2bk, b2k = ins["best2"]
        lb1 = BK_LABELS.get(b1bk, b1bk)
        lb2 = BK_LABELS.get(b2bk, b2bk)
        reasons.append(
            f"Лучшие кефы: на <b>{name1}</b> — {b1k:.2f} ({lb1}), "
            f"на <b>{name2}</b> — {b2k:.2f} ({lb2})."
        )
        imp1, imp2 = ins["implied1"], ins["implied2"]
        reasons.append(
            f"Рынок оценивает шансы: {name1} — {imp1:.0f}%, {name2} — {imp2:.0f}% "
            f"(без маржи БК)."
        )
        if pick:
            # ---- V107: симметричный статус пика против рынка ----
            model_prob_pick = ins["model1"] if pick == name1 else 100.0 - ins["model1"]
            market_prob_pick = imp1 if pick == name1 else imp2
            divergence = model_prob_pick - market_prob_pick
            if divergence >= config.PREDICT_MAX_DIVERGENCE:
                stance = "divergence"
            elif divergence <= -3:
                stance = "caution"
            elif divergence >= 3:
                stance = "value"
            else:
                stance = "agree"
            ins["pick_stance"] = stance
            ins["model_prob_pick"] = model_prob_pick
            ins["market_prob_pick"] = market_prob_pick
            ins["divergence"] = divergence
            if stance == "divergence":
                # Урезаем самоуверенность: модель против рынка на >= 15 п.п. —
                # почти наверняка данные неполны (ростер, календарь, мотивация).
                confidence = min(confidence, int(config.PREDICT_CONF_CAP_DIVERGENCE))
                reasons.append(
                    f"⚠️ РАСХОЖДЕНИЕ С РЫНКОМ: модель даёт {pick} {model_prob_pick:.0f}% "
                    f"против {market_prob_pick:.0f}% у БК ({divergence:+.0f} п.п.). "
                    f"Перекос такого размера — почти всегда слепое пятно статистики "
                    f"карт (ростер, уровень соперников, мотивация). Пик рискованный."
                )
            else:
                edge_pick = ins["edge1"] if pick == name1 else -ins["edge1"]
                if stance == "value":
                    reasons.append(
                        f"VALUE: модель даёт {pick} выше рынка на {edge_pick:+.0f} п.п. — "
                        f"по кефу есть перевес."
                    )
                elif stance == "caution":
                    reasons.append(
                        f"ОСТОРОЖНО: рынок грузит {pick} сильнее модели ({edge_pick:+.0f} п.п.) — "
                        f"кеф уже, чем оправдывает статистика."
                    )
                else:
                    reasons.append("Модель и рынок согласны — без явного перевеса.")
    else:
        reasons.append("Кефов на эту пару в линиях БК не нашлось — рынок не учтён.")

    # ---------------- итоговая строка ----------------
    if pick:
        if ins.get("pick_stance") == "divergence":
            base = (f"Пик: <b>{pick}</b>, но модель сильно противоречит рынку — "
                    f"уверенность снижена до {confidence}%. "
                    f"Перед ставкой разберись в контексте (ростер, мотивация).")
        else:
            base = (f"Ставка на победу <b>{pick}</b> — уверенность {confidence}%. "
                    f"{pick_word} по совокупности статистики карт и личных встреч.")
        if ins.get("best1"):
            best_bk, best_k = (ins["best1"] if pick == name1 else ins["best2"])
            base += (f" Лучший кеф: {best_k:.2f} "
                     f"({BK_LABELS.get(best_bk, best_bk)}).")
        verdict = base
    elif not has1 and not has2:
        # V108: пустота не должна маскироваться под «команды равны»
        verdict = ("Данных по командам нет — честный прогноз дать нельзя. "
                   "Ориентируйся на кефы рынка или пропусти матч.")
    else:
        verdict = ("Команды равны по статистике — чистый исход рискован, "
                   "смотри тоталы и исходы по картам.")

    return {
        "pick": pick,
        "pick_word": pick_word,
        "confidence": confidence,
        "low_data": low_data,
        "reasons": reasons,
        "verdict": verdict,
        "avg1": avg1, "avg2": avg2,
        "best1": best1, "best2": best2,
        "h2h": h2h,
        "power1": power1, "power2": power2,
        "edge_map": edge_map, "edge_diff": edge_diff, "edge_leader": edge_leader,
        "odds": ins,
        "form1": f1v, "form2": f2v,
        "map_forecast": [
            {"map": m, "wr1": round(maps1[m]), "wr2": round(maps2[m])}
            for m in sorted(common, key=lambda x: -abs(maps1[x] - maps2[x]))[:4]
        ],
        "roster_alert": bool(alert1 or alert2),
        "rank1": rank1, "rank2": rank2,
        "players_info": {
            "p1": ps1[1] if ps1 else None,
            "p2": ps2[1] if ps2 else None,
        },
    }


def plain_text_reasons(p: dict) -> str:
    """Причины без HTML — для подписи к фото (Telegram caption не поддерживает <b>)."""
    import re as _re
    return "\n".join("• " + _re.sub(r"</?b>", "", r) for r in p["reasons"])
