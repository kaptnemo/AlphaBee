import time
import logging
from typing import Any

from pandas import DataFrame

from alphabee.collectors.akshare import ak

try:
    from alphabee.utils import get_logger
except ModuleNotFoundError:
    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)

MONGO_URI = "mongodb://root:cyw271828@localhost:27017/"
MONGO_DATABASE = "treasure_island"
logger = get_logger(__name__)


def get_mongo_database() -> Any:
    """Create the default MongoDB database handle lazily."""
    try:
        import pymongo
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "pymongo is required to save AkShare results to MongoDB."
        ) from exc
    return pymongo.MongoClient(MONGO_URI)[MONGO_DATABASE]


class AkShareResult:
    """Wrap AkShare result and provide common export operations."""

    def __init__(self, data: Any, collection_name: str):
        self._data = data
        self._collection_name = collection_name

    @property
    def data(self) -> Any:
        return self._data

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def to_dataframe(self) -> DataFrame:
        """Normalize the AkShare result into a DataFrame."""
        if isinstance(self._data, DataFrame):
            return self._data
        if isinstance(self._data, dict):
            return DataFrame([self._data])
        if isinstance(self._data, list):
            return DataFrame(self._data)
        raise TypeError(
            f"AkShare result for '{self._collection_name}' cannot be converted to DataFrame: "
            f"{type(self._data).__name__}"
        )

    def save_to_mongo(
        self,
        collection_name: str | None = None,
        replace: bool = True,
        db: Any = None,
    ) -> None:
        """Save data to MongoDB using the API name as the default collection."""
        dataframe = self.to_dataframe()
        if not dataframe.empty:
            collection_name = collection_name or self._collection_name
            mongo_db = db or get_mongo_database()
            if replace:
                mongo_db.drop_collection(collection_name)
            mongo_db[collection_name].insert_many(dataframe.to_dict("records"))

    def save_to_csv(self, file_path: str, index: bool = False):
        """Save data to CSV."""
        self.to_dataframe().to_csv(file_path, index=index)

    def save_to_parquet(self, file_path: str, index: bool = False):
        """Save data to parquet."""
        self.to_dataframe().to_parquet(file_path, index=index)


class AkShareHelper:
    """Fetch data from AkShare and wrap the result with common helpers."""

    def __init__(self):
        self.akshare_api = ak

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @staticmethod
    def wrap_akshare_result(func, name):
        """Wrap AkShare API call with retry logic."""

        def wrapper(*arg, **kwargs):
            max_retries = 1
            for attempt in range(max_retries):
                try:
                    return AkShareResult(func(*arg, **kwargs), name)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(
                            "AkShare API failed after max retries",
                            api=name,
                            attempts=max_retries,
                            error=str(e),
                        )
                        raise
                    wait = attempt + 1
                    logger.warning(
                        "AkShare API error, retrying",
                        api=name,
                        attempt=attempt + 1,
                        wait_seconds=wait,
                        error=str(e),
                    )
                    time.sleep(wait)

        return wrapper

    def __getattr__(self, name: str) -> Any:
        return self.wrap_akshare_result(getattr(self.akshare_api, name), name)


if __name__ == "__main__":
    with AkShareHelper() as helper:
        # result = helper.stock_zh_a_daily(symbol="sh600000")
        # result.save_to_csv("sh600000_daily.csv")
        # result.save_to_parquet("sh600000_daily.parquet")
        # print(result.data.head())

        # stock_news_df = helper.stock_news_main_cx()
        # print(stock_news_df.data.head())
        stock_news_df = helper.stock_news_em(symbol="药明康德")
        print(stock_news_df.data.head())

        names = helper.stock_board_industry_name_em()
        print(names.data.head())