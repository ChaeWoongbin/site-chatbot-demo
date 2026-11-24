from dataclasses import dataclass
from os import getenv
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Settings:
    port: int = 7001
    data_dir: Path = PROJECT_ROOT / "data"
    gemini_api_key: str = ""
    gemini_model: str = ""
    upload_token: str = ""
    chat_ip_per_minute: int = 10
    chat_ip_per_hour: int = 60
    chat_global_per_minute: int = 60
    chat_global_per_day: int = 100


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        port=int(getenv("PORT", "7001")),
        gemini_api_key=getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=getenv("GEMINI_MODEL", "").strip(),
        upload_token=getenv("DEMO_UPLOAD_TOKEN", "").strip(),
    )
