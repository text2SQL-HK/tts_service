from pathlib import Path
import json
import os

from dotenv import load_dotenv

#유틸
def load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")

#설정 값들
APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
load_dotenv(BASE_DIR / ".env")

PIPELINE_PATH = APP_DIR / "services" / "tts_pipeline.py"
CHAT_PATH = APP_DIR / "services" / "chat.py"
DB_LIST_PATH = APP_DIR / "metadata" / "db_list.json"
with open(DB_LIST_PATH, "r", encoding="utf-8") as f:
    loaded = json.load(f)
tables_data = loaded if isinstance(loaded, list) else [loaded]
DB_LIST = [table_data["db_id"] for table_data in tables_data]

PLOT_IMAGE_NAME = "plot"
PLOT_TMP_DIR = "/home"
PROMPT_DIR = APP_DIR / "prompts"
CHATBOT_SYS_PROMPT = load_prompt(PROMPT_DIR / "SYS_PROMPT-mod.txt")
STATIC_DIR = APP_DIR / "static"
SQL_ROWS_PAGE_SIZE = 200
RERANK_URL = "http://127.0.0.1:8002/rerank"
SESSION_TIMEOUT_SECONDS = 3600 # 1시간
ORACLE_JSON_PATH = Path(APP_DIR / "metadata" / "ORACLE_INFO.json")

POSTGRE_HOST = os.getenv("POSTGRE_HOST", "127.0.0.1")
POSTGRE_NAME = os.getenv("POSTGRE_DB") or os.getenv("POSTGRE_NAME", "")
POSTGRE_USER = os.getenv("POSTGRE_USER", "")
POSTGRE_PWD = os.getenv("POSTGRE_PWD", "")
POSTGRE_PORT = int(os.getenv("POSTGRE_PORT", "5432"))
POSTGRE_POOL_MIN_CONN = int(os.getenv("POSTGRE_POOL_MIN_CONN", "1"))
POSTGRE_POOL_MAX_CONN = int(os.getenv("POSTGRE_POOL_MAX_CONN", "10"))

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(SESSION_TIMEOUT_SECONDS)))
CACHE_HISTORY_LIMIT = int(os.getenv("CACHE_HISTORY_LIMIT", "100"))
CACHE_EVENTS_LIMIT = int(os.getenv("CACHE_EVENTS_LIMIT", "100"))

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "session_token")
LOGIN_AUTH_ME_URL = os.getenv("LOGIN_AUTH_ME_URL", "http://127.0.0.1:8001/api/auth/me")

TTS_UI_PREFIX = os.getenv("TTS_UI_PREFIX", "/tts")
TTS_STATIC_PREFIX = os.getenv("TTS_STATIC_PREFIX", "/tts/static")
TTS_API_PREFIX = os.getenv("TTS_API_PREFIX", "/api/tts")
LOGIN_UI_PREFIX = os.getenv("LOGIN_UI_PREFIX", "/login")
