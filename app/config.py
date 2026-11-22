import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI", "http://localhost:8000/callback")
SCOPE = os.getenv("LINKEDIN_SCOPE", "openid profile w_member_social")
LINKEDIN_VERSION = os.getenv("LINKEDIN_VERSION", "202610")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
API_KEY = os.getenv("API_KEY", "")  # empty = REST API disabled
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./scheduler.db")
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
