from pydantic import BaseModel, Field


class AlphaBeeState(BaseModel):
    user_id: str | None = None
    user_query: str

    intent: str | None = None
    symbol: str | None = None
    market: str = "A_SHARE"

    market_data: dict = Field(default_factory=dict)
    news_data: list[dict] = Field(default_factory=list)
    fundamental_analysis: dict = Field(default_factory=dict)
    technical_analysis: dict = Field(default_factory=dict)
    drisk_analysis: dict = Field(default_factory=dict)
    strategy_result: dict = Field(default_factory=dict)

    errors: list[str] = Field(default_factory=list)
    final_answer: str | None = None
