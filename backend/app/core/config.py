"""Configuracion central de la aplicacion (variables de entorno)."""

from functools import lru_cache

from pydantic import model_validator
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

    # Consulta del titular por documento (E1-T35, HU01): proxy server-side a
    # https://app.apiinti.dev/api/v1 (GET /dni/{numero}, /ruc/{numero}) con
    # `Authorization: Bearer <APIINTI_API_KEY>`. La key vive en el `.env` de
    # la raiz y llega al contenedor por `docker-compose.yml`; nunca al cliente.
    apiinti_base_url: str = "https://app.apiinti.dev/api/v1"
    apiinti_api_key: str = ""
    doc_lookup_provider: str = "mock"
    doc_lookup_mock_mode: str = "success"
    doc_lookup_timeout_seconds: float = 5.0
    doc_lookup_max_retries: int = 2
    doc_lookup_backoff_base_seconds: float = 0.1
    doc_lookup_breaker_failures: int = 3
    doc_lookup_breaker_cooldown_seconds: float = 30.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _reject_default_jwt_secret_outside_local(self) -> "Settings":
        """Rechaza el arranque si `jwt_secret` es vacio o el default fuera de `local`.

        Solo compara contra el valor por defecto (`change-me`); nunca registra
        ni expone el valor configurado. En `local` se permite el default para
        no romper tests/desarrollo.
        """
        if str(self.app_env).lower() != "local" and (
            not self.jwt_secret or self.jwt_secret.strip() == "" or self.jwt_secret == "change-me"
        ):
            raise ValueError(
                "JWT_SECRET debe ser un secreto aleatorio/fuerte "
                "cuando APP_ENV no es 'local' (el valor por defecto no es valido)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
