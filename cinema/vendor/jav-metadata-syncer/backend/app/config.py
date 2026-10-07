"""Configuration loaded from env (or .env file)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cors_origins: str = "*"

    # 啟用的來源（逗號分隔）；停用的來源不參與搜尋
    enabled_sources: str = "javbus,javtrailers,missav"

    # 目錄（Docker 內以 env 覆寫為 /app/data、/app/output）
    data_dir: str = "./data"
    output_dir: str = "./output"


settings = Settings()
