# settings.py — compatible con Pydantic v1 y v2

from typing import Optional, Dict
from pathlib import Path
import sentry_sdk

# Intentar primero pydantic v2 (pydantic-settings); si no, fallback a v1
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # v2
    _V2 = True
except Exception as e:
    sentry_sdk.capture_exception(e)
    from pydantic import BaseSettings  # v1
    from pydantic import Extra
    SettingsConfigDict = None
    _V2 = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    # ---------------- Base de datos ----------------
    MYSQL_USER: str = "bot"
    MYSQL_PASS: str = "botpass"
    MYSQL_HOST: str = "localhost"
    MYSQL_DB:   str = "botpelu"

    # ---------------- OpenAI ----------------
    OPENAI_API_KEY: str = "changeme"

    # ---------------- Calendario / TZ ----------------
    CAL_TZ: str = "Europe/Madrid"
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "credentials.json"

    # ---------------- WhatsApp Cloud API ----------------
    WABA_VERIFY_TOKEN: str = "changeme"
    WABA_APP_SECRET: str = "changeme"
    WABA_TOKEN: Optional[str] = None
    GRAPH_API_VERSION: str = "v23.0"

    # ---------------- Estado / Rate limit ----------------
    STORAGE_BACKEND: str = "memory"  # "memory" | "redis"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Límites TEXTUALES (Flask-Limiter)
    GLOBAL_PER_IP: str = "200/minute"      # <-- FALTABA ESTE CAMPO
    USER_RATE: str = "100/minute"
    HEAVY_ENDPOINT: str = "100/minute"
    WEBHOOK_PER_PELU: str = "1500/minute"

    # Límites NUMÉRICOS (los usas con int(...))
    RATE_LIMIT_PER_MIN: int = 1500
    OUTBOUND_WA_PER_PELU: int = 100
    OUTBOUND_WA_PER_USER: int = 70

    STRICT_LOCKS: bool = True
    LOOPBACK_TIMEOUT_SECONDS: int = 40

    @property
    def RATE_LIMITS(self) -> Dict[str, object]:
        """
        Unifica los límites que usa la app.
        - Strings tipo 'X/minute' para Flask-Limiter.
        - Enteros para límites numéricos propios.
        """
        return {
            "GLOBAL_PER_IP": self.GLOBAL_PER_IP,
            "USER_RATE": self.USER_RATE,               # <-- ANTES ponías "USER"
            "HEAVY_ENDPOINT": self.HEAVY_ENDPOINT,
            "WEBHOOK_PER_PELU": self.WEBHOOK_PER_PELU,
            "OUTBOUND_WA_PER_PELU": int(self.OUTBOUND_WA_PER_PELU),
            "OUTBOUND_WA_PER_USER": int(self.OUTBOUND_WA_PER_USER),
        }

    # Carga .env
    if _V2:
        model_config = SettingsConfigDict(
            env_file=str(PROJECT_ROOT / ".env"),
            env_file_encoding="utf-8",
            extra="ignore",
        )
    else:
        class Config:
            env_file = str(PROJECT_ROOT / ".env")
            extra = Extra.ignore

settings = Settings()
