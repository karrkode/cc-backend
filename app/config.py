"""Settings, read from environment variables (or backend/.env when running locally).

On AWS the container gets these from Secrets Manager at start-up; nothing secret
lives in the image or the repo.
"""
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"  # local | test | prod
    database_url: str = "postgresql+psycopg://culture:culture@localhost:5432/culture"

    jwt_secret: str = "change-me-local-only"
    jwt_issuer: str = "chiculturecrew"
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # Comma-separated in the environment, e.g. "https://chiculturecrew.com,http://localhost:4173"
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:4173", "http://127.0.0.1:4173"]
    cookie_secure: bool = False  # True in prod (HTTPS only)
    cookie_domain: str | None = None  # ".chiculturecrew.com" in prod so the API and page share it

    login_attempts_per_minute: int = 10

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _real_secret_in_prod(cls, v, info):
        if info.data.get("env") == "prod" and (v == "change-me-local-only" or len(v) < 32):
            raise ValueError("JWT_SECRET must be set to a random value of 32+ characters in prod")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
