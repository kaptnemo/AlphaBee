import time
from typing import Any

# import pymongo
from pandas import DataFrame

from alphabee.adapters.tushare import TuShare_Adapter
from alphabee.collectors.tushare import ts
from alphabee.utils import get_logger

# mongo_client: pymongo.MongoClient[dict[str, Any]] = pymongo.MongoClient(
#     "mongodb://root:cyw271828@localhost:27017/"
# )
# db = mongo_client["treasure_island"]
logger = get_logger(__name__)


def _report_tushare_failure(api_name: str, exc: Exception, kwargs: dict) -> None:
    """Record a Tushare API failure in the data_fetch event database."""
    try:
        from alphabee.data_fetch.integrations import _classify_error, capture_failure

        symbol = kwargs.get("ts_code", "")
        capture_failure(
            provider="tushare",
            api_name=api_name,
            symbol=symbol if symbol else None,
            error_type=_classify_error(exc),
            error_message=str(exc),
            request_payload={k: v for k, v in kwargs.items()},
        )
    except Exception:
        pass  # never let failure recording break the caller


class TuShareResult:
    """wrapper tushare result, add common operations"""

    def __init__(self, data: DataFrame, collection_name: str):
        self._data = TuShare_Adapter.adapt(collection_name, data)
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

    # Error substrings that indicate a permanent failure (no retry will help)
    _PERMANENT_ERROR_KEYWORDS: tuple[str, ...] = (
        "权限",
        "没有接口",
        "token",
        "认证",
    )

    def __init__(self):
        self.tushare_api = ts.pro_api()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @classmethod
    def _is_permanent_error(cls, error_message: str) -> bool:
        """Return True when an error is permanently non-retryable (permission, auth, etc.)."""
        return any(kw in error_message for kw in cls._PERMANENT_ERROR_KEYWORDS)

    @staticmethod
    def wrap_tushare_result(func, name):
        """wrap tushare api with retry logic (max 10 attempts, incremental backoff)

        Args:
            func (method): tushare api method
            name (str): tushare api name used as collection name
        """

        def wrapper(*arg, **kwargs):
            max_retries = 1
            e = None
            for attempt in range(1, max_retries + 1):
                try:
                    return TuShareResult(func(*arg, **kwargs), name)
                except Exception as e:
                    err_msg = str(e)
                    # Permission / auth errors are permanent — fail fast, no retry
                    if TuShareHelper._is_permanent_error(err_msg):
                        logger.error(
                            "Tushare API permanent error (no retry)",
                            api=name,
                            error=err_msg,
                        )
                        _report_tushare_failure(name, e, kwargs)
                        raise

                    if attempt == max_retries:
                        logger.error(
                            "Tushare API failed after max retries",
                            api=name,
                            attempts=max_retries,
                            error=err_msg,
                        )
                        _report_tushare_failure(name, e, kwargs)
                        raise
                    wait = attempt**2  # 1s, 4s, 9s
                    logger.warning(
                        "Tushare API error, retrying",
                        api=name,
                        attempt=attempt,
                        wait_seconds=wait,
                        error=err_msg,
                    )
                    time.sleep(wait)
            else:
                logger.error("Tushare API failed after max retries", api=name)
                if e is not None:
                    raise e
                else:
                    raise RuntimeError(f"Tushare API {name} failed after {max_retries} attempts")

        return wrapper

    def __getattr__(self, name: str) -> Any:
        return self.wrap_tushare_result(getattr(self.tushare_api, name), name)


if __name__ == "__main__":
    ts_client = TuShareHelper()
    # result = ts_client.index_weight(
    #     index_code="399300.SZ", start_date="20221201", end_date="20221231"
    # )
    # logger.info("Fetched index weight result", result=str(result))
    # logger.info("Fetched index weight dataframe", rows=len(result.data))

    all_stocks = ts_client.stock_basic(exchange="", list_status="L")
    all_stocks.save_to_csv("/data/freedom/AlphaBee/alphabee/static/all_stocks.csv")

    # df = ts_client.balancesheet(
    #     ts_code='600000.SH',
    #     start_date='20170101',
    #     end_date='20260608',
    #     fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,cap_rese,total_assets'
    # ).data
    # print(df)
