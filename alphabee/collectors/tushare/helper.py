import time
from typing import Any
# import pymongo

from pandas import DataFrame
from alphabee.collectors.tushare import ts
from alphabee.utils import get_logger

# mongo_client: pymongo.MongoClient[dict[str, Any]] = pymongo.MongoClient(
#     "mongodb://root:cyw271828@localhost:27017/"
# )
# db = mongo_client["treasure_island"]
logger = get_logger(__name__)


class TuShareResult:
    """wrapper tushare result, add common operations"""

    def __init__(self, data: DataFrame, collection_name: str):
        self._data = data
        self._collection_name = collection_name

    @property
    def data(self) -> DataFrame:
        return self._data

    @property
    def collection_name(self) -> str:
        return self._collection_name

    # def save_to_mongo(
    #     self,
    #     collection_name: str | None = None,
    #     replace: bool = True,
    #     db: Any = None,
    # ) -> None:
    #     """save data to mongo, the collection name is the method name which get the data"""
    #     if not self._data.empty:
    #         collection_name = collection_name or self._collection_name
    #         mongo_collection = (db or globals()["db"])[collection_name]
    #         mongo_collection.insert_many(self._data.to_dict("records"))

    def save_to_csv(self, file_path: str, index: bool = False):
        """save data to csv"""
        self._data.to_csv(file_path, index=index)

    def save_to_parquet(self, file_path: str, index: bool = False):
        """save data to parquet"""
        self._data.to_parquet(file_path, index=index)


class TuShareHelper:
    """Fetch data from tushare, and handler the result"""

    def __init__(self):
        self.tushare_api = ts.pro_api()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @staticmethod
    def wrap_tushare_result(func, name):
        """wrap tushare api with retry logic (max 10 attempts, incremental backoff)

        Args:
            func (method): tushare api method
            name (str): tushare api name used as collection name
        """

        def wrapper(*arg, **kwargs):
            max_retries = 10
            for attempt in range(max_retries):
                try:
                    return TuShareResult(func(*arg, **kwargs), name)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(
                            "Tushare API failed after max retries",
                            api=name,
                            attempts=max_retries,
                            error=str(e),
                        )
                        raise
                    wait = attempt + 1  # 1s, 2s, ..., 9s
                    logger.warning(
                        "Tushare API error, retrying",
                        api=name,
                        attempt=attempt + 1,
                        wait_seconds=wait,
                        error=str(e),
                    )
                    time.sleep(wait)

        return wrapper

    def __getattr__(self, name: str) -> Any:
        return self.wrap_tushare_result(getattr(self.tushare_api, name), name)


if __name__ == "__main__":
    ts_client = TuShareHelper()
    result = ts_client.index_weight(
        index_code="399300.SZ", start_date="20221201", end_date="20221231"
    )
    logger.info("Fetched index weight result", result=str(result))
    logger.info("Fetched index weight dataframe", rows=len(result.data))

    all_stocks = ts_client.stock_basic(exchange="", list_status="L")
    all_stocks.save_to_csv("/data/freedom/AlphaBee/alphabee/static/all_stocks.csv")
