from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        enable_decoding=False,
    )

    app_env: str = "local"
    max_bot_token: str = Field(default="", alias="MAX_BOT_TOKEN")
    max_bot_username: str = Field(default="", alias="MAX_BOT_USERNAME")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    webhook_path: str = Field(default="/webhook", alias="WEBHOOK_PATH")
    storage_backend: str = Field(default="ydb", alias="STORAGE_BACKEND")
    ydb_endpoint: str = Field(
        default="grpc://localhost:2136",
        alias="YDB_ENDPOINT",
    )
    ydb_database: str = Field(default="/local", alias="YDB_DATABASE")
    ydb_metadata_credentials: bool = Field(
        default=False,
        alias="YDB_METADATA_CREDENTIALS",
    )
    source_database_url: str = Field(default="", alias="SOURCE_DATABASE_URL")
    admin_user_ids: list[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    organizer_user_ids: list[int] = Field(
        default_factory=list,
        alias="ORGANIZER_USER_IDS",
    )
    max_api_rps: int = Field(default=30, alias="MAX_API_RPS")
    documents_version: str = Field(
        default="hackathon-2026-05",
        alias="DOCUMENTS_VERSION",
    )

    @field_validator("admin_user_ids", "organizer_user_ids", mode="before")
    @classmethod
    def parse_ids(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
