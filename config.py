"""
config.py — настройки проекта.
Все ключи берутся из .env, чтобы не хардкодить секреты в коде.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Telegram ---
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0") or 0)

# --- Общие настройки ---
TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")

# false = видимый браузер (рекомендуется на Windows, Cloudflare пропускает надёжнее)
# true  = фоновый режим (для сервера)
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")

# V111fix: максимум секунд на запуск Camoufox. Playwright по умолчанию ждёт
# 180 сек и всё это время держит блокировку — другие браузерные задачи
# (матчи, кефы, HLTV) стояли в очереди по 3 минуты, бот выглядел зависшим.
# Если у тебя машина медленно стартует Firefox — подними в .env,
# например: CAMOUFOX_LAUNCH_TIMEOUT=90
CAMOUFOX_LAUNCH_TIMEOUT = float(os.getenv("CAMOUFOX_LAUNCH_TIMEOUT", "60") or 60)

# --- Пути ---
PROFILE_DIR = str(BASE_DIR / "hltv_profile")   # профиль Camoufox (куки/сессия HLTV)
DB_PATH = str(BASE_DIR / "cs2_bot.db")
CACHE_DIR = BASE_DIR / "cache"
LOGOS_DIR = CACHE_DIR / "logos"

# --- TTL кэша (секунды): сколько жить данным до повторного сбора ---
MATCHES_TTL = 3 * 3600        # список матчей на день
MAPS_TTL = 12 * 3600          # винрейты карт команды
H2H_TTL = 24 * 3600           # личные встречи пары
ODDS_TTL = 10 * 60            # коэффициенты БК (живые данные, кэш короткий)

# --- Букмекерские конторы (Этап 2) ---
# V105: Фонбет сменил API (не отвечает), Бетбум задушен антиботом — выключены.
# Оставлены Пари (HTTP, мгновенно) и Винлайн (браузер, кэш 10 мин).
# Вернуть любую можно, дописав её имя в .env: ODDS_ENABLED=pari,winline,fonbet,betboom
ODDS_ENABLED = [x.strip() for x in os.getenv(
    "ODDS_ENABLED", "pari,winline").split(",") if x.strip()]

# Страницы киберлинии для браузерных БК (Винлайн/Бетбум).
# Пари и Фонбет берутся напрямую JSON-запросом — страницы им не нужны.
ODDS_SITES = {
    "fonbet": {"urls": []},  # JSON API напрямую, страница не нужна
    "pari": {"urls": []},    # JSON API напрямую, страница не нужна
    "winline": {"urls": [
        "https://winline.ru/stavki/sport/kibersport",
        "https://winline.ru/line/kiber",
    ]},
    "betboom": {"urls": [
        "https://betboom.ru/sports/esports/counter-strike",
        "https://betboom.ru/line/esports",
        "https://betboom.ru/line",
    ]},
}

# --- Этап 3: value-модель ---
VALUE_MIN_EDGE = 3.0          # минимальный перевес модели над рынком, п.п.
VALUE_ODDS_TTL = 30 * 60      # каким свежим считать кефы для value-скана (сек)

# --- V107: защита прогнозной модели ---
# Активный пул CS2 с 08.07.2026 (Cache заменил Overpass; Anubis вернулся в
# январе вместо Train). Карты вне пула не участвуют в расчёте силы — винрейты
# Overpass/Train взяты из старого пула и на банне сейчас не встретятся.
ACTIVE_DUTY_MAPS = ["Ancient", "Anubis", "Cache", "Dust2", "Inferno", "Mirage", "Nuke"]
# Карта учитывается в модели, только если команда сыграла на ней >= N матчей
# (отсекает шум вида "Cache 100%" после одной игры).
MIN_MAP_GAMES = 3
# Расхождение модели с рынком (п.п.), при котором пик считается подозрительным:
# уверенность урезается и в карточку идёт предупреждение.
PREDICT_MAX_DIVERGENCE = 15.0
# Потолок уверенности при подозрительном расхождении с рынком.
PREDICT_CONF_CAP_DIVERGENCE = 62
# Value-скан: сигнал блокируется, если модель расходится с рынком сильнее, чем
# на это число п.п. (перекос такого размера почти всегда — ошибка данных).
VALUE_MAX_DIVERGENCE = 20.0
# Максимальный размер ставки по сигналу, % банка (даже если ¼ Келли больше).
VALUE_MAX_STAKE_PCT = 2.0

# --- V108: стабильность сбора и честность прогноза ---
# Команда/пара, по которым источник недавно не дал данных, повторно
# не дёргаются этот интервал (раньше каждый клик заново гонял 10 неудачных
# запросов по тем же командам — шторм в логах).
WARMUP_FAIL_COOLDOWN = 30 * 60
# V114: надолго не долбим то, что не вылечится само: "не найдена на bo3.gg",
# "нет карт в истории" (мёртвые/отсутствующие команды) — повтор раз в сутки.
WARMUP_FAIL_TTL_RESOLUTION = 24 * 3600
# Отрицательный кэш поиска bo3.gg: имя -> не искать повторно (сек).
# Ставится ТОЛЬКО когда bo3.gg честно ответил «команды нет»/«истории нет».
BO3_MISS_TTL = 24 * 3600
# V115: сетевая ошибка (таймаут/429/сбой) — это НЕ «команда не найдена».
# Раньше любой сетевой чих клал команду в 24-часовой miss — и она выпадала
# из прогнозов на сутки. Теперь сетевые сбои кэшируются на 15 минут.
BO3_NET_TTL = 15 * 60
WARMUP_MIN_INTERVAL = 120      # полный прогрев не чаще, чем раз в 2 минуты
BG_HLTV_MIN_INTERVAL = 300     # фоновая HLTV-сверка не чаще раза в 5 минут
# Потолок уверенности, когда по одной из команд НЕТ ни карт, ни формы:
# модель видит только одну сторону — не даём ей выдавать 80-90% на пустоте.
PREDICT_CONF_CAP_LOWDATA = 58
# V115: строка «мало данных» (форма есть не у всех / мало карт в выборке):
# сигнал по такому матчу признаётся только при повышенном эдже.
VALUE_MIN_EDGE_PARTIAL = 8.0

# --- V109: шринкейдж винрейтов карт по размеру выборки -------------------
# Bayesian-приор: винрейт карты с n играми подтягивается к 50% с весом
# MAP_SHRINK_PRIOR. "Cache 100%" после 2 игр станет (100*2+50*3)/5 = 70%,
# а карта на 20 игр почти не изменится. Данные об играх приходят из bo3.gg.
MAP_SHRINK_PRIOR = 3.0

# --- V109/V110: AI-аналитик (бесплатные провайдеры, OpenAI-совместимый API) ---
# groq.com    (рекомендую): AI_API_BASE=https://api.groq.com/openai/v1
#                         AI_MODEL=openai/gpt-oss-120b
#                         ВНИМАНИЕ: llama-3.3-70b-versatile Groq снял с сервиса
#                         16.08.2026 (перевёл на Enterprise). Если в .env остался
#                         старая модель — бот при 404 сам переключится на первую
#                         доступную из AI_MODEL_FALLBACKS и напишет это в лог.
# gemini    (Google AI Studio): AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
#                         AI_MODEL=gemini-2.5-flash
# openrouter: AI_API_BASE=https://openrouter.ai/api/v1
#                         AI_MODEL=<выбрать на openrouter.ai/models (фильтр free)>
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.groq.com/openai/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
# V111fix: llama-3.3-70b-versatile Groq снял с сервиса 16.08.2026. Если она
# осталась в .env — молча подменяем на актуальную, чтобы каждый старт бота
# не начинался с 404 и WARNING. Править .env не обязательно.
if AI_MODEL.lower() in ("llama-3.3-70b-versatile", "llama-3.3-70b"):
    AI_MODEL = "openai/gpt-oss-120b"
# Запасные модели: если основную снимут с сервиса, бот сам возьмёт первую
# доступную из этого списка (список моделей запрашивается у того же API).
AI_MODEL_FALLBACKS = [x.strip() for x in os.getenv(
    "AI_MODEL_FALLBACKS",
    "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if x.strip()]
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "45") or 45)
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "900") or 900)
AI_TEMP = float(os.getenv("AI_TEMP", "0.4") or 0.4)
AI_CACHE_TTL = 15 * 60          # повторный AI-разбор той же пары — мгновенно из кэша

# --- V109: монитор ростеров (живая аналитика) ----------------------------
# Как часто перепроверять составы команд матчей дня (PandaScore, HTTP).
ROSTER_TTL = 4 * 3600
# Сколько дней свежий алерт о смене ростера ещё влияет на прогноз.
ROSTER_ALERT_DAYS = 7
# Потолок уверенности, когда у команды недавно менялся ростер: статистика
# карт/формы собрана в основном старым составом и может врать.
ROSTER_CONF_CAP = 65

# --- V109: новости команд (бесплатный поиск DuckDuckGo) ------------------
NEWS_MAX_ITEMS = 8
NEWS_DAYS = 14                  # глубина поиска новостей, дней

# --- V113: глубокие данные HLTV (рейтинги игроков, мировой ранг) ----------
# Рейтинг игроков с HLTV (/stats/players?team=): rating 2.1 за 3 месяца.
# Кэш в БД (таблица player_stats), сколько считать свежим:
PLAYER_STATS_TTL = 12 * 3600
# Сколько месяцев статистики игроков запрашивать у HLTV.
PLAYER_STATS_MONTHS = 3
# Игрок с меньшим числом карт за период в модель силы не идёт (шум).
PLAYER_MIN_MAPS = 15
# Мировой ранг HLTV (/ranking/teams): одна страница на весь топ-30,
# кэш надолго — ранг меняется только с выходом нового топа.
RANK_TTL = 5 * 24 * 3600
# Прогрев глубоких данных: максимум команд за раз (берутся команды дня).
DEEP_WARMUP_MAX = 16
# После неудачи сбора команды глубокие данные повторно не дёргаются (сек).
DEEP_FAIL_COOLDOWN = 30 * 60

# --- V111: Турниры и Лайв-центр ------------------------------------------
# Максимум турниров в списке (остальные — под «Другие турниры»).
TOURNAMENTS_MAX = 20
# Режим слежения без scorebot: как часто опрашивать источники (сек).
LIVE_WATCH_POLL = 25
# Авто-стоп слежения (мин) — чтобы задача не висела вечно.
LIVE_WATCH_MAX_MINUTES = 240

# --- V111: HLTV scorebot (живые данные матча: раунд, деньги, закупы) ------
# 0 в .env (LIVE_SCOREBOT=0) выключает мост, бот живёт на опросе источников.
SCOREBOT_ENABLED = os.getenv("LIVE_SCOREBOT", "1").lower() in ("1", "true", "yes")
# Мин. пауза между правками лайв-карточки (сек) — защита от флуд-лимитов TG.
SCOREBOT_EDIT_GAP = 4.0
# Классификация закупа по СРЕДНИМ деньгам на игрока (примерно):
SB_FULL_BUY = 4500              # >= полный закуп
SB_SEMI_BUY = 3100              # >= форс/полу-закуп
SB_MINI_BUY = 2000              # >= 2к-бай, ниже — эко

# --- V114: слежение-дайджест (звёздочка на матче) ---------------------------
# Как часто фоновый цикл проверяет отмеченные матчи (сек).
FOLLOW_POLL = 60
# Мин. пауза между дайджестами по одному матчу (сек).
FOLLOW_DIGEST_GAP = 120
# Сколько матчей можно помечать одновременно (на чат).
FOLLOW_MAX_PER_CHAT = 6

# --- Этап 3.5 (V105): внешние источники данных ---
# PandaScore — первичный HTTP-источник матчей/лайва (без браузера).
PANDASCORE_TOKEN = os.getenv("PANDASCORE_TOKEN", "").strip()
# API-Sport — резервный источник; ключи через запятую, ротация автоматом.
APISPORT_TOKENS = [x.strip() for x in os.getenv("APISPORT_TOKENS", "").split(",") if x.strip()]
APISPORT_DAILY_LIMIT = int(os.getenv("APISPORT_DAILY_LIMIT", "100") or 100)
# Кросс-проверка обоих источников при КАЖДОМ сборе (тратит квоту API-Sport).
# 0 = API-Sport только как страховка, когда PandaScore упал (по умолчанию).
APISPORT_CROSSCHECK = os.getenv("APISPORT_CROSSCHECK", "0").lower() in ("1", "true", "yes")
MATCHES_SRC_TTL = 10 * 60     # кэш списка матчей из внешних источников (сек)
LIVE_TTL = 75                 # кэш лайв-матчей (сек)
FUZZY_MIN = 87                # порог нечёткого сопоставления команд (0-100)

# --- HLTV ---
HLTV_BASE = "https://www.hltv.org"

for _d in (CACHE_DIR, LOGOS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
