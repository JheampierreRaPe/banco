"""Configuracion central de la aplicacion (variables de entorno)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Banca Online Integral"
    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://banca:banca@localhost:5432/banca"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    cors_origins: str = "http://localhost:3000,http://localhost:8080"

    kyc_base_url: str = "http://localhost:8000"
    kyc_api_key: str = "change-me"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
