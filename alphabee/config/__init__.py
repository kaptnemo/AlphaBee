from pathlib import Path

from pydantic import BaseModel, DirectoryPath

from alphabee.config.loader import ConfigLoader


class LLMConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    proxy_url: str | None = None


class Settings(BaseModel):
    llm: LLMConfig


def get_settings() -> Settings:
    config_loader = ConfigLoader()
    raw_cfg = config_loader.load_config()
    return Settings(**raw_cfg)


settings = get_settings()
