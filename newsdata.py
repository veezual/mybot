"""
newsdata.py — V109: новости команд через бесплатный поиск DuckDuckGo.

Без API-ключей: пакет ddgs (наследник duckduckgo_search) ходит в публичный
поиск. Пакет опциональный: если не установлен — бот честно скажет «поиск
недоступен», ничего не упадёт (импорт защищён).

Запрос выполняется в отдельном потоке (asyncio.to_thread) — библиотека
блокирующая, и не должна тормозить цикл событий aiogram.
"""
import asyncio
import logging

import config

log = logging.getLogger("newsdata")

_DDGS = None          # ленивый импорт
_ddgs_failed = False


def _get_ddgs():
    global _DDGS, _ddgs_failed
    if _ddgs_failed:
        return None
    if _DDGS is None:
        try:
            from ddgs import DDGS          # новый пакет (2025+)
            _DDGS = DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS   # старое имя пакета
                _DDGS = DDGS
            except ImportError:
                _ddgs_failed = True
                return None
    return _DDGS


def ddgs_available() -> bool:
    return _get_ddgs() is not None


def _search_sync(query: str, max_results: int, timelimit: str) -> list[dict]:
    DDGS = _get_ddgs()
    if not DDGS:
        return []
    items = []
    with DDGS() as d:
        try:
            # ddgs>=9: первый позиционный параметр query
            rows = list(d.news(query, region="ru-ru",
                               timelimit=timelimit, max_results=max_results))
        except TypeError:
            # duckduckgo_search / старые ddgs: параметр keywords
            rows = list(d.news(keywords=query, region="ru-ru",
                               timelimit=timelimit, max_results=max_results))
        for r in rows:
            items.append({
                "title": (r.get("title") or "").strip(),
                "body": (r.get("body") or "").strip(),
                "source": (r.get("source") or "").strip(),
                "date": (r.get("date") or "").strip(),
                "url": (r.get("url") or "").strip(),
            })
    return items


async def search_news(query: str, max_results: int | None = None,
                      days: int | None = None) -> list[dict]:
    """Новости по запросу. Пусто = не нашлось/пакета нет/поиск лёг."""
    max_results = max_results or config.NEWS_MAX_ITEMS
    days = days or config.NEWS_DAYS
    timelimit = "w" if days <= 7 else "m"
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, max_results, timelimit),
            timeout=25)
    except asyncio.TimeoutError:
        log.info("news: поиск %r завис >25с", query)
        return []
    except Exception as e:
        log.info("news: поиск %r не удался: %s", query, e)
        return []
