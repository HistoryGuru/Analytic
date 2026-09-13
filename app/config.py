"""
Centralized app configuration, loaded from environment variables (.env locally,
Render's dashboard env vars in production).
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Tabroom
    tabroom_username: str = ""
    tabroom_password: str = ""

    # Opencaselist
    opencaselist_username: str = ""
    opencaselist_password: str = ""

    # Groq
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Season config
    caselist_slug: str = "hsld26"

    # Cache / CORS
    cache_ttl_seconds: int = 900
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_origin_list(self) -> List[str]:
        return [c.strip() for c in self.cors_origins.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
