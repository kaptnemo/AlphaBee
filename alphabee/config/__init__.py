from pydantic import BaseModel, Field

from alphabee.config.loader import ConfigLoader


class LLMConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    proxy_url: str | None = None


class TavilyConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.tavily.com"
    proxy_url: str | None = None
    timeout: float = Field(default=15.0, description="请求超时秒数")
    max_results: int = Field(default=6, description="默认返回结果数")


class DDGSConfig(BaseModel):
    proxy_url: str | None = None
    timeout: int = Field(default=20, description="请求超时秒数")
    region: str = Field(default="cn-zh", description="搜索区域，如 cn-zh, us-en")
    max_results: int = Field(default=6, description="默认返回结果数")


class WebSearchConfig(BaseModel):
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    ddgs: DDGSConfig = Field(default_factory=DDGSConfig)


class DataConfig(BaseModel):
    root_dir: str = Field(default="data", description="数据产物根目录")


class Settings(BaseModel):
    llm: LLMConfig
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    data: DataConfig = Field(default_factory=DataConfig)


def get_settings() -> Settings:
    config_loader = ConfigLoader()
    raw_cfg = config_loader.load_config()
    return Settings(**raw_cfg)


settings = get_settings()
