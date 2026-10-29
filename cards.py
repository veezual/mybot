"""
cards.py — генерация красивых PNG-карточек (стиль strogostavit_bot: тёмная тема,
акцентные бары статистики, логотипы команд, блок прогноза с объяснением).

Работает на Windows (Segoe UI / Arial) и Linux (DejaVu Sans) без доп. зависимостей.
"""
import io
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from config import LOGOS_DIR, TZ_NAME
from config import ACTIVE_DUTY_MAPS as _ACTIVE_MAPS   # V112: фильтр пула

log = logging.getLogger("cards")

# ---------------------------------------------------------------- палитра
BG_TOP = (11, 15, 25)
BG_BOTTOM = (22, 29, 46)
PANEL = (26, 35, 52)
PANEL_LIGHT = (34, 45, 66)
ACCENT = (88, 101, 242)      # индиго
ACCENT2 = (94, 214, 255)     # циан
GREEN = (63, 185, 80)
RED = (248, 81, 73)
GOLD = (255, 193, 69)
TEXT = (235, 240, 250)
MUTED = (139, 148, 172)

W = 1080

# ---------------------------------------------------------------- шрифты

_FONT_CACHE = {}


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = ([
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\segoeuil.ttf" if not bold else r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ] if _is_windows() else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"


def _text(draw, xy, text, size, color=TEXT, bold=False, anchor=None, spacing=6):
    draw.text(xy, text, font=_load_font(size, bold), fill=color, anchor=anchor)


def _wrap(text: str, size: int, max_width: int) -> list[str]:
    font = _load_font(size)
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if draw_text_len(test, font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_text_len(text: str, font) -> float:
    try:
        return font.getlength(text)
    except AttributeError:
        return font.getsize(text)[0]


# ---------------------------------------------------------------- примитивы

def _base_card(title: str, subtitle: str, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, height), BG_TOP)
    # вертикальный градиент
    top = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        top.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    img = Image.new("RGB", (W, height))
    img.paste(top.resize((W, height)), (0, 0))

    draw = ImageDraw.Draw(img)
    # акцентная полоса сверху
    draw.rectangle([0, 0, W, 6], fill=ACCENT)

    _text(draw, (48, 42), title, 46, TEXT, bold=True)
    if subtitle:
        _text(draw, (48, 104), subtitle, 26, MUTED)
    # разделитель
    draw.line([48, 156, W - 48, 156], fill=(60, 72, 100), width=2)
    return img, draw


def _panel(draw, box, radius=22, fill=PANEL):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _bar(draw, x, y, width, height, pct: float, color=ACCENT, bg=(52, 63, 88)):
    draw.rounded_rectangle([x, y, x + width, y + height], radius=height // 2, fill=bg)
    fill_w = int(width * max(0.0, min(100.0, pct)) / 100.0)
    if fill_w > height:
        draw.rounded_rectangle([x, y, x + fill_w, y + height], radius=height // 2, fill=color)


def _logo_or_initials(name: str, logo_url: str | None, size: int) -> Image.Image:
    """Логотип из кэша/сети, иначе кружок с инициалами."""
    key = hashlib.md5((logo_url or name).encode()).hexdigest()[:16]
    cache_file = LOGOS_DIR / f"{key}.png"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGBA").resize((size, size))
        except Exception:
            pass
    if logo_url:
        try:
            import requests
            resp = requests.get(logo_url, timeout=8,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.ok and resp.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1") \
                    or (resp.ok and resp.headers.get("content-type", "").startswith("image/")):
                im = Image.open(io.BytesIO(resp.content)).convert("RGBA").resize((size, size))
                im.save(cache_file)
                return im
        except Exception as e:
            log.debug("logo %s: %s", name, e)

    # инициалы
    initials = "".join(w[0] for w in name.split()[:2]).upper() or name[:2].upper()
    color = ACCENT2 if int(hashlib.md5(name.encode()).hexdigest(), 16) % 2 else ACCENT
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, size - 1, size - 1], fill=color)
    f = _load_font(int(size * 0.42), bold=True)
    d.text((size / 2, size / 2), initials, font=f, fill=(10, 14, 24), anchor="mm")
    return im


def _paste_logo(img, name, logo_url, cx, cy, size):
    logo = _logo_or_initials(name, logo_url, size)
    # мягкая тень
    shadow = Image.new("RGBA", (size + 24, size + 24), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse([12, 12, size + 12, size + 12], fill=(0, 0, 0, 140))
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    img.paste(shadow, (cx - (size + 24) // 2, cy - (size + 24) // 2), shadow)
    img.paste(logo, (cx - size // 2, cy - size // 2), logo)


def _now_str() -> str:
    return datetime.now(tz=ZoneInfo(TZ_NAME)).strftime("%d.%m.%Y, %H:%M")


# ---------------------------------------------------------------- карточки

def team_card(team_name: str, logo_url: str | None,
              maps: dict[str, float]) -> bytes:
    # V112: показываем ТОЛЬКО активный пул (Overpass/Train/Vertigo давно
    # не играются — пользователь прав: их статистика только мусорит)
    maps = {m: v for m, v in (maps or {}).items()
            if m in _ACTIVE_MAPS or m.upper() in {a.upper() for a in _ACTIVE_MAPS}}
    top_maps = sorted(maps.items(), key=lambda x: -x[1])[:7]
    maps_top = 490
    height = maps_top + len(top_maps) * 92 + 150
    img, draw = _base_card("СТАТИСТИКА КАРТ", team_name, height)

    cy = 210
    _paste_logo(img, team_name, logo_url, W - 170, 290, 170)

    avg = sum(maps.values()) / len(maps) if maps else 0
    best, worst = (top_maps[0], top_maps[-1]) if top_maps else (("—", 0), ("—", 0))
    for i, (label, value) in enumerate([
        ("Средний винрейт", f"{avg:.1f}%"),
        ("Лучшая карта", f"{best[0]} · {best[1]:.1f}%"),
        ("Худшая карта", f"{worst[0]} · {worst[1]:.1f}%"),
    ]):
        yy = 210 + i * 84
        _panel(draw, [48, yy, 700, yy + 64])
        _text(draw, (76, yy + 12), label, 24, MUTED)
        _text(draw, (76, yy + 34), value, 28, TEXT, bold=True)

    y = maps_top
    for map_name, wr in top_maps:
        _panel(draw, [48, y, W - 48, y + 76])
        _text(draw, (76, y + 24), map_name, 30, TEXT, bold=True)
        _bar(draw, 400, y + 28, 480, 20, wr,
             GREEN if wr >= 55 else (GOLD if wr >= 48 else RED))
        _text(draw, (930, y + 20), f"{wr:.1f}%", 30,
              GREEN if wr >= 55 else (GOLD if wr >= 48 else RED), bold=True)
        y += 92

    _text(draw, (48, height - 96), "bo3.gg · только активный пул карт · " + _now_str(), 22, MUTED)
    _text(draw, (48, height - 60), "Винрейт = доля побед на карте за последний период", 22, MUTED)

    return _to_bytes(img)


def _norm_nick(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _rows_with_ratings(roster_names, pstats) -> list[tuple[str, float | None]]:
    """V113: строки блока составов: [(ник, рейтинг|None), ...].

    Никниги — из ростера (PandaScore), рейтинг подставляется по совпадению
    ника из HLTV-статистики. Если ростера нет — ники берём из самих рейтингов.
    """
    rmap = {_norm_nick(p.get("nickname")): p for p in (pstats or [])
            if p.get("nickname") and p.get("rating") is not None}
    names = [n for n in (roster_names or []) if n]
    if not names:
        names = [p["nickname"] for p in (pstats or [])[:5]]
    rows = []
    for nm in names[:5]:
        p = rmap.get(_norm_nick(nm))
        rows.append((nm, (p or {}).get("rating")))
    return rows


def match_card(m1name: str, m1logo: str | None, m1maps: dict[str, float],
               m2name: str, m2logo: str | None, m2maps: dict[str, float],
               pred: dict, start_time: str = "",
               rosters: tuple[list[str], list[str]] | None = None,
               players: tuple[list[dict], list[dict]] | None = None,
               ranks: tuple[int | None, int | None] | None = None) -> bytes:
    h2h = pred["h2h"]

    # заранее считаем переносы строк объяснений, чтобы карточка не переполнялась.
    # Берём первые (статистика) и последние (кефы/рынок) — всегда сбалансированно.
    all_reasons = pred["reasons"]
    if len(all_reasons) > 6:
        picked = all_reasons[:3] + all_reasons[-3:]
    else:
        picked = all_reasons
    reason_blocks = []
    for r in picked:
        clean = r.replace("<b>", "").replace("</b>", "")
        reason_blocks.append(_wrap("• " + clean, 25, W - 150))

    odds_rows = (pred.get("odds") or {}).get("rows") or []

    # V113: рейтинги игроков + ранги
    p1v, p2v = ([], [])
    if players:
        p1v, p2v = players[0] or [], players[1] or []
    rk1, rk2 = (ranks if ranks else (pred.get("rank1"), pred.get("rank2")))
    rows1 = _rows_with_ratings((rosters or (None, None))[0], p1v)
    rows2 = _rows_with_ratings((rosters or (None, None))[1], p2v)
    has_ratings = any(r is not None for _, r in rows1) or any(r is not None for _, r in rows2)

    # V112: высота блока составов (до 5 ников на команду, в 2 колонки)
    rosters_h = 0
    r1 = [x[0] for x in rows1]
    r2 = [x[0] for x in rows2]
    n_rows = max(len(rows1), len(rows2))
    if n_rows:
        rosters_h = 52 + n_rows * 38 + 10 + 14

    y_flow = 694
    y_flow += rosters_h
    if h2h["total"]:
        y_flow += 100
    odds_height = (56 + len(odds_rows) * 44 + 14) if odds_rows else 0
    y_flow += odds_height
    y_flow += 52  # заголовок "ПОЧЕМУ ТАК"
    reasons_height = sum(len(lines) * 30 + 22 + 14 for lines in reason_blocks)
    height = y_flow + reasons_height + 140
    img, draw = _base_card("ПРОГНОЗ НА МАТЧ", start_time or "CS2 · HLTV", height)

    # -------- блок команд
    _panel(draw, [48, 196, W - 48, 420])
    _paste_logo(img, m1name, m1logo, 190, 300, 130)
    _paste_logo(img, m2name, m2logo, W - 190, 300, 130)

    _text(draw, (W // 2, 258), "VS", 44, MUTED, bold=True, anchor="mm")

    def _fit(name: str, size=32):
        f = _load_font(size, bold=True)
        while draw_text_len(name, f) > 320 and size > 20:
            size -= 2
            f = _load_font(size, bold=True)
        return f

    draw.text((190, 396), m1name, font=_fit(m1name), fill=TEXT, anchor="ms")
    draw.text((W - 190, 396), m2name, font=_fit(m2name), fill=TEXT, anchor="ms")

    # средние винрейты + V113: бейдж мирового ранга HLTV
    _text(draw, (190, 428),
          (f"HLTV #{rk1} · " if rk1 else "") + f"{pred['avg1']:.1f}% карт",
          24, ACCENT2, anchor="mm")
    _text(draw, (W - 190, 428),
          (f"HLTV #{rk2} · " if rk2 else "") + f"{pred['avg2']:.1f}% карт",
          24, ACCENT2, anchor="mm")

    # -------- вердикт
    verdict_fill = GREEN if pred["pick"] else GOLD
    _panel(draw, [48, 452, W - 48, 560], fill=PANEL_LIGHT)
    _text(draw, (76, 470), "ВЕРДИКТ", 24, MUTED, bold=True)
    verdict_txt = (f"{pred['pick']} · уверенность {pred['confidence']}%"
                   if pred["pick"] else "Без явного фаворита")
    _text(draw, (76, 500), verdict_txt, 36, verdict_fill, bold=True)

    # -------- силы (шкала)
    total_power = pred["power1"] + pred["power2"] or 1
    share = pred["power1"] / total_power
    _panel(draw, [48, 590, W - 48, 664])
    _text(draw, (76, 606), m1name[:22], 22, TEXT)
    _text(draw, (W - 76, 606), m2name[:22], 22, TEXT, anchor="ra")
    bar_x, bar_y, bar_w, bar_h = 76, 634, W - 152, 16
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=8, fill=(52, 63, 88))
    draw.rounded_rectangle([bar_x, bar_y, bar_x + int(bar_w * share), bar_y + bar_h],
                           radius=8, fill=ACCENT)

    y = 694
    # -------- составы + рейтинги игроков (V113)
    if rows1 or rows2:
        panel_h = 52 + n_rows * 38 + 10
        _panel(draw, [48, y, W - 48, y + panel_h])
        head = ("СОСТАВЫ · РЕЙТИНГ HLTV (3 МЕС.)" if has_ratings else "СОСТАВЫ")
        _text(draw, (76, y + 10), head, 22, MUTED, bold=True)
        yy = y + 44
        for i in range(n_rows):
            if i < len(rows1):
                nm, rat = rows1[i]
                _text(draw, (76, yy), nm[:20], 26, TEXT)
                if rat is not None:
                    col = GREEN if rat >= 1.15 else (GOLD if rat >= 1.05 else RED)
                    _text(draw, (W // 2 - 110, yy), f"{rat:.2f}", 26, col,
                          bold=True, anchor="ra")
            if i < len(rows2):
                nm, rat = rows2[i]
                _text(draw, (W - 76, yy), nm[:20], 26, TEXT, anchor="ra")
                if rat is not None:
                    col = GREEN if rat >= 1.15 else (GOLD if rat >= 1.05 else RED)
                    _text(draw, (W // 2 + 110, yy), f"{rat:.2f}", 26, col,
                          bold=True, anchor="la")
            yy += 38
        y += panel_h + 14
    if h2h["total"]:
        _panel(draw, [48, y, W - 48, y + 76])
        _text(draw, (76, y + 10), "ЛИЧНЫЕ ВСТРЕЧИ", 22, MUTED, bold=True)
        _text(draw, (76, y + 36),
              f"{h2h['w1']} : {h2h['w2']}", 30,
              GREEN if h2h["w1"] >= h2h["w2"] else RED, bold=True)
        if h2h["streak"] >= 2 and h2h["streak_name"]:
            _text(draw, (W - 76, y + 38), f"серия {h2h['streak_name']}: {h2h['streak']}",
                  24, GOLD, anchor="ra")
        y += 100

    # -------- кефы БК (Этап 2)
    if odds_rows:
        panel_h = 56 + len(odds_rows) * 44
        _panel(draw, [48, y, W - 48, y + panel_h])
        _text(draw, (76, y + 10), "КЕФЫ БК · П1 / П2", 22, MUTED, bold=True)
        yy = y + 38
        for row in odds_rows:
            best = row.get("best")
            _text(draw, (76, yy + 6), row["label"], 25, TEXT, bold=True)
            _text(draw, (W - 300, yy + 6), f"{row['k1']:.2f}", 25,
                  GOLD if best == 1 else TEXT, bold=(best == 1))
            _text(draw, (W - 150, yy + 6), f"{row['k2']:.2f}", 25,
                  GOLD if best == 2 else TEXT, bold=(best == 2))
            yy += 44
        y += panel_h + 14

    # -------- объяснение
    _text(draw, (48, y + 4), "ПОЧЕМУ ТАК", 26, ACCENT2, bold=True)
    y += 52
    for lines in reason_blocks:
        _panel(draw, [48, y, W - 48, y + max(52, len(lines) * 30 + 22)], fill=PANEL)
        for j, ln in enumerate(lines):
            _text(draw, (76, y + 12 + j * 30), ln, 25, TEXT)
        y += max(52, len(lines) * 30 + 22) + 14

    _text(draw, (48, height - 96),
          "Статистика: HLTV / bo3.gg · рейтинги игроков: HLTV (3 мес.) · составы: PandaScore",
          22, MUTED)
    _text(draw, (48, height - 60), f"Не является финансовой рекомендацией · {_now_str()}", 22, MUTED)

    return _to_bytes(img)


def h2h_card(name1: str, logo1: str | None,
             name2: str, logo2: str | None,
             summary: dict) -> bytes:
    rows = summary["last"]
    height = 620 + len(rows) * 96 + 140
    img, draw = _base_card("ЛИЧНЫЕ ВСТРЕЧИ", f"{name1} vs {name2}", height)

    _panel(draw, [48, 196, W - 48, 380])
    _paste_logo(img, name1, logo1, 190, 280, 120)
    _paste_logo(img, name2, logo2, W - 190, 280, 120)
    big = f"{summary['w1']} : {summary['w2']}"
    _text(draw, (W // 2, 262), big, 64,
          GREEN if summary["w1"] >= summary["w2"] else GOLD, bold=True, anchor="mm")
    _text(draw, (W // 2, 330), f"победы {name1} : победы {name2}", 24, MUTED, anchor="mm")
    if summary["streak"] >= 2 and summary["streak_name"]:
        _text(draw, (W // 2, 358), f"серия {summary['streak_name']}: {summary['streak']} подряд",
              24, GOLD, anchor="mm")

    y = 420
    if not rows:
        _panel(draw, [48, y, W - 48, y + 90])
        _text(draw, (76, y + 30), "Матчей между командами не найдено.", 26, MUTED)
        y += 110

    for r in rows:
        _panel(draw, [48, y, W - 48, y + 82])
        _text(draw, (76, y + 12), r.get("date", ""), 22, MUTED)
        t1 = r.get("t1", "")
        t2 = r.get("t2", "")
        sc = r.get("score", "?")
        try:
            import re as _re
            mm = _re.search(r"(\d+)\s*[-–—:]\s*(\d+)", sc)
            left, right = (int(mm.group(1)), int(mm.group(2))) if mm else (0, 0)
        except Exception:
            left, right = 0, 0
        winner_is_left = left > right

        title = f"{t1}  {sc}  {t2}"
        f = _load_font(27, bold=True)
        while draw_text_len(title, f) > 760 and f.size > 18:
            f = _load_font(f.size - 1, bold=True)
        draw.text((76, y + 38), title, font=f, fill=TEXT)
        _text(draw, (W - 76, y + 38), r.get("event", ""), 22, MUTED, anchor="ra")
        # цвет счёта победителя
        if r.get("winner") == name1:
            draw.rounded_rectangle([48, y, 56, y + 82], radius=4, fill=GREEN)
        elif r.get("winner") == name2:
            draw.rounded_rectangle([W - 56, y, W - 48, y + 82], radius=4, fill=GREEN)
        y += 96

    _text(draw, (48, height - 80), "Данные: HLTV.org · " + _now_str(), 22, MUTED)
    return _to_bytes(img)


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
