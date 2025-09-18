# config.py
import os
from dotenv import load_dotenv
from datetime import timedelta, timezone
import sys

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

load_dotenv()

# -------------------------
# API Server
# -------------------------
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "5000"))

# -------------------------
# Chrome / WhatsApp
# -------------------------
CHROME_HEADLESS = os.getenv("CHROME_HEADLESS", "false").lower() == "true"
BROWSER_ROOT_DIR = os.getenv("BROWSER_ROOT_DIR", "Browser")
WHATSAPP_URL = os.getenv("WHATSAPP_URL", "https://web.whatsapp.com/")
LOGIN_TIMEOUT_SECONDS = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "120"))

# -------------------------
# Capacity / Autoscaling
# -------------------------
TASKS_PER_PROFILE = int(os.getenv("TASKS_PER_PROFILE", "10"))
SCALE_INTERVAL_SECONDS = int(os.getenv("SCALE_INTERVAL_SECONDS", "5"))
PROFILE_START_DELAY_SECONDS = int(os.getenv("PROFILE_START_DELAY_SECONDS", "4"))

# -------------------------
# Database
# -------------------------
DB_HOSTNAME = os.getenv("DB_HOSTNAME", "").strip()
DB_NAME = os.getenv("DB_NAME", "").strip()
DB_USERNAME = os.getenv("DB_USERNAME", "").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "").strip()
DB_PORT = os.getenv("DB_PORT", "").strip()
DB_CHARSET = os.getenv("DB_CHARSET", "utf8mb4").strip()

# Eksik bilgi kontrolü

if not all([DB_HOSTNAME, DB_NAME, DB_USERNAME, DB_PASSWORD, DB_PORT]):
    sys.exit("[HATA] Veritabanı bilgileri eksik! Lütfen .env dosyasındaki DB_HOSTNAME, DB_NAME, DB_USERNAME, DB_PASSWORD, DB_PORT alanlarını doldurun.")

DATABASE_URL = f"mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOSTNAME}:{DB_PORT}/{DB_NAME}?charset={DB_CHARSET}"

# -------------------------
# Time Zone
# -------------------------
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Istanbul")
APP_TZ_OFFSET_HOURS = int(os.getenv("APP_TZ_OFFSET_HOURS", "3"))

def _get_app_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(APP_TIMEZONE)
        except Exception:
            pass
    # Fallback: fixed offset timezone (useful if zoneinfo is unavailable)
    return timezone(timedelta(hours=APP_TZ_OFFSET_HOURS))

APP_TZ = _get_app_tz()

# -------------------------
# Logging
# -------------------------
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# -------------------------
# Initial Profiles
# -------------------------
INITIAL_PROFILES = int(os.getenv("INITIAL_PROFILES", "1"))
