"""Settings, read from environment variables (or backend/.env when running locally).

On AWS the container gets these from Secrets Manager at start-up; nothing secret
lives in the image or the repo.
"""
from functools import lru_cache
from typing import Annotated, Literal

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

    # Account emails (CC-44): address verification and password reset. See app/email.py.
    # email_backend: ses (send through Amazon SES), log (write the email to the log; local),
    # memory (keep it for tests), none (skip). Unset: log when ENV=local, none otherwise.
    email_backend: Literal["ses", "log", "memory", "none"] | None = None
    ses_region: str = "us-east-1"  # where the SES identity lives (infra/aws/ses.tf)
    mail_from: str = "Culture Crew <no-reply@chiculturecrew.com>"
    mail_reply_to: str = "info@chiculturecrew.com"  # replies reach a real inbox; no-reply@ has none
    site_url: str = "http://localhost:4173"  # links in emails open the site, never the API
    recap_to: str = "info@chiculturecrew.com"  # the weekly discovery recap (CC-77)
    recap_name: str = "Andrew"  # its greeting, "Hello, Andrew!" (CC-81)
    verify_link_hours: int = 24
    reset_link_minutes: int = 60
    # Turn on once real users can receive the email (SES production access): until then an
    # unverified account could never be verified, so it would be locked out of submitting.
    require_verified_email: bool = False

    # Local only: the test accounts scripts.dev_users creates (see docker-compose.yml). Never set in prod.
    dev_admin_email: str = ""
    dev_admin_password: str = ""
    dev_user_email: str = ""
    dev_user_password: str = ""

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


    @property
    def email_mode(self) -> str:
        return self.email_backend or ("log" if self.env == "local" else "none")


@lru_cache
def get_settings() -> Settings:
    return Settings()
