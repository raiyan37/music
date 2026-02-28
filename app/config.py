from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    musicgpt_api_key: str = ""
    musicgpt_api_base: str = "https://api.musicgpt.com/api/public"
    upload_dir: Path = Path("./uploads")
    output_dir: Path = Path("./outputs")

    poll_interval_seconds: float = 5.0
    poll_timeout_seconds: float = 600.0

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
