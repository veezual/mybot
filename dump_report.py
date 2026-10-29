"""
dump_report.py — компактный отчёт по дампам из папки debug_odds.

Зачем: дампы бывают огромными (HTML до 200 КБ), в чат такое не вставить.
Скрипт сжимает ВСЕ дампы в один маленький файл dump_report.txt:
  • карта структуры JSON (пути, типы, размеры, примеры значений);
  • начало не-JSON данных (HTML и т.п.).

Этого хватает, чтобы докрутить парсер. Дальнейшее — по запросу.

Запуск: dump_report.bat (двойной клик) или:
    venv\\Scripts\\activate.bat
    python dump_report.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DEBUG_DIR = Path("debug_odds")
OUT_FILE = Path("dump_report.txt")

PER_FILE_BUDGET = 4000   # максимум символов на один дамп
TOTAL_BUDGET = 60000     # максимум символов на весь отчёт


def _sample(v, limit=80) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[:limit] + "..."


def structure_map(o, max_lines=110, max_depth=5) -> list[str]:
    """Карта структуры: путь -> тип/размер/пример. Компактно и информативно."""
    lines: list[str] = []

    def walk(v, path: str, depth: int):
        if len(lines) >= max_lines or depth > max_depth:
            if len(lines) == max_lines:
                lines.append("... (карта обрезана)")
            return
        if isinstance(v, dict):
            items = list(v.items())
            for k, vv in items[:25]:
                p = f"{path}.{k}" if path else str(k)
                if isinstance(vv, (dict, list)):
                    lines.append(f"{p} : {'dict' if isinstance(vv, dict) else 'list'}"
                                 f"[{len(vv)}]")
                    walk(vv, p, depth + 1)
                else:
                    lines.append(f"{p} = {_sample(vv)}")
            if len(items) > 25:
                lines.append(f"{path} : ...ещё {len(items) - 25} ключей")
        elif isinstance(v, list):
            for i, vv in enumerate(v[:3]):
                p = f"{path}[{i}]"
                if isinstance(vv, (dict, list)):
                    lines.append(f"{p} : {'dict' if isinstance(vv, dict) else 'list'}"
                                 f"[{len(vv)}]")
                    walk(vv, p, depth + 1)
                else:
                    lines.append(f"{p} = {_sample(vv)}")
            if len(v) > 3:
                lines.append(f"{path} : ...ещё {len(v) - 3} элементов (показаны [0..2])")

    walk(o, "", 0)
    return lines


def render_one(path: Path, budget: int) -> str:
    """Отчёт по одному дамп-файлу в рамках бюджета символов."""
    head = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"### {path.name}\nНЕ ЧИТАЕТСЯ: {e!r}\n"

    url = note = ""
    data = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            url = str(parsed.get("url", ""))[:160]
            note = str(parsed.get("note", ""))[:160]
            data = parsed.get("data", parsed)
    except Exception:
        data = raw  # не JSON — считаем сырым текстом (HTML и т.п.)

    head.append(f"### {path.name}")
    if url:
        head.append(f"url: {url}")
    if note:
        head.append(f"note: {note}")
    if isinstance(data, str):
        size = len(data)
        head.append(f"тип: строка (длина {size}, похоже на HTML)"
                    if "<" in data[:200] else f"тип: строка (длина {size})")
        body = data[:budget - 600]
        return "\n".join(head) + "\n--- начало данных ---\n" + body + "\n"

    # JSON: карта структуры + первые символы как есть
    lines = structure_map(data)
    body_map = "\n".join(lines)
    out = "\n".join(head) + "\n--- карта структуры ---\n" + body_map
    if len(out) < budget - 700:
        try:
            raw_json = json.dumps(data, ensure_ascii=False)[:700]
            out += "\n--- начало данных ---\n" + raw_json
        except Exception:
            pass
    return out + "\n"


def main() -> int:
    if not DEBUG_DIR.is_dir():
        print("Папки debug_odds нет — дампы не сохранились, отчёт не нужен.")
        return 0
    files = sorted(DEBUG_DIR.glob("*.json"))
    if not files:
        print("В debug_odds нет *.json — отчёт не нужен.")
        return 0

    stamp = datetime.now().strftime("%d.%m %H:%M")
    parts = [f"CS2 BOT DEBUG REPORT  ({stamp})  файлов: {len(files)}"]
    used = len(parts[0])
    for f in files:
        chunk = render_one(f, PER_FILE_BUDGET)
        if used + len(chunk) > TOTAL_BUDGET:
            parts.append(f"### {f.name}\n(не поместился в отчёт — расскажу отдельно)\n")
            continue
        parts.append(chunk)
        used += len(chunk)

    report = "\n".join(parts)
    OUT_FILE.write_text(report, encoding="utf-8")
    print(f"Готово: {OUT_FILE} ({len(report)} символов, {len(files)} дамп(ов)).")
    try:
        os.startfile(OUT_FILE)  # Windows: открыть в Блокноте
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
