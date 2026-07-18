"""
Baostock Helper Module
This module provides a helper class for interacting with the Baostock API,
including methods for logging in, querying daily and minute stock data,
and saving results to MongoDB.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import baostock as bs  # type: ignore[import-untyped]
import pandas as pd
from alpha_arena.utils import get_logger
from pymongo import MongoClient

MONGO_HOST = "mongodb://root:cyw271828@localhost:27017/"
MONGO_DATABASE = "prosera"
logger = get_logger(__name__)


@contextlib.contextmanager
def get_mongo_client() -> Iterator[MongoClient[Any]]:
    """Get a MongoDB client instance."""
    client: MongoClient[Any] = MongoClient(MONGO_HOST)
    with client:
        yield client


class BaostockResult:
    """A class to encapsulate the result of a Baostock query.
    This class holds the data, error code, error message, table name,
    timestamp column, and tag columns for the result.
    It also provides a method to save the result to MongoDB.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        table_name: str,
        timestamp_column: str | None = None,
        tag_columns: list[str] | None = None,
    ) -> None:
        self.data = data
        self.table_name = table_name
        self.tag_columns = tag_columns
        self.timestamp_column = timestamp_column

    def _save_to_mongo(
        self,
        collection_name: str,
        replace: bool,
        client: MongoClient[Any],
    ) -> None:
        """Internal method to save data to MongoDB.
        Args:
            collection_name (str): The name of the MongoDB collection.
            replace (bool): Whether to replace the existing collection.
            client (MongoClient): The MongoDB client instance.
        """
        if not self.data.empty:
            if replace:
                client[MONGO_DATABASE].drop_collection(collection_name)
            mongo_collection = client[MONGO_DATABASE][collection_name]
            mongo_collection.insert_many(self.data.to_dict("records"))
        else:
            logger.warning("No data to save to MongoDB", collection_name=collection_name)

    def save_to_mongo(
        self,
        collection_name: str | None = None,
        replace: bool = False,
        client: MongoClient[Any] | None = None,
    ) -> None:
        """save data to mongo, the collection name is the method name which get the data
        Args:
            collection_name (str): The name of the MongoDB collection to save the data.
            replace (bool): Whether to replace the existing collection.
            client (MongoClient): The MongoDB client instance. If not provided, a new client will be created.
            if client is provided, it will be used to save the data, and the close method will not be called.
        """
        collection_name = collection_name or self.table_name
        if client:
            self._save_to_mongo(collection_name, replace, client)
        else:
            with MongoClient(MONGO_HOST) as client:
                self._save_to_mongo(collection_name, replace, client)

    def save_to_csv(self, file_path: str):
        """Save the data to a CSV file.
        Args:
            file_path (str): The path to the CSV file where the data will be saved.
        """
        if not self.data.empty:
            self.data.to_csv(file_path, index=False)
        else:
            logger.warning("No data to save to CSV", file_path=file_path)

    def save_to_parquet(self, file_path: str):
        """Save the data to a Parquet file.
        Args:
            file_path (str): The path to the Parquet file where the data will be saved.
        """
        if not self.data.empty:
            self.data.to_parquet(file_path, index=False)
        else:
            logger.warning("No data to save to Parquet", file_path=file_path)


class BaostockHelper:
    """A helper class for interacting with the Baostock API.
    This class provides methods for logging in, querying daily and minute stock data,
    and saving results to MongoDB.
    It can be used as a context manager to ensure proper login and logout.
    """

    def __enter__(self):
        """Initialize BaostockHelper and login to Baostock."""
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Logout from Baostock when exiting the context."""
        # self.logout()
        pass

    @staticmethod
    def login():
        """Login to Baostock and return the login response."""
        lg = bs.login()
        if lg.error_code != "0":
            raise Exception(f"Login failed: {lg.error_msg}")
        return lg

    @staticmethod
    def logout():
        """Logout from Baostock."""
        bs.logout()

    def daily(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjustflag: str = "2",
    ) -> BaostockResult:
        """Fetch daily data for a given stock code."""
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise Exception(f"Query failed: {rs.error_msg}")

        # Convert the 'time' column to datetime format
        # and set the frequency
        df = rs.get_data()
        if not df.empty:
            df["code"] = code
            df["time"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
            df["frequency"] = "d"
            df.set_index("time", inplace=True)
        else:
            df = pd.DataFrame()

        return BaostockResult(df, table_name="stock_ohlcv", tag_columns=["code", "frequency", "date"])

    def minute(
        self,
        code: str,
        start_date: str,
        end_date: str,
        frequency: str = "5",
        adjustflag: str = "2",
    ) -> BaostockResult:
        """Fetch minute data for a given stock code."""
        rs = bs.query_history_k_data_plus(
            code,
            "date,time,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise Exception(f"Query failed: {rs.error_msg}")

        # Convert the 'time' column to datetime format
        # and set the frequency
        df = rs.get_data()
        if not df.empty:
            df["code"] = code
            df["time"] = pd.to_datetime(df["time"], format="%Y%m%d%H%M%S000")
            df["frequency"] = f"{frequency}min"
            df.set_index("time", inplace=True)
        else:
            df = pd.DataFrame()

        return BaostockResult(df, table_name="stock_ohlcv", tag_columns=["code", "frequency", "date"])

    def all_stocks(self) -> BaostockResult:
        """Fetch all stock codes available in Baostock."""
        rs_basic = bs.query_stock_basic()
        if rs_basic.error_code != "0":
            raise Exception(f"Query failed: {rs_basic.error_msg}")

        df_basic = rs_basic.get_data()
        df_stock = df_basic[df_basic["type"] == "1"]

        return BaostockResult(df_stock, table_name="stocks")

    def query_csi300_stocks(self, date: str | None = None) -> BaostockResult:
        """Fetch stock codes for the csi300 index."""
        rs_csi300 = bs.query_hs300_stocks(date=date)
        if rs_csi300.error_code != "0":
            raise Exception(f"Query failed: {rs_csi300.error_msg}")

        df_csi300 = rs_csi300.get_data()
        return BaostockResult(df_csi300, table_name="csi300_stocks")


if __name__ == "__main__":
    # Example usage
    with BaostockHelper() as helper:
        # daily_result = helper.daily("sh.600008", "2020-09-01", "2020-09-28")
        # daily_result.save_to_influx(daily_result.table_name)

        # minute_result = helper.minute("sh.600008", "2020-09-01", "2020-09-28")
        # minute_result.save_to_influx(minute_result.table_name)

        # logger.info("Example run start")
        # all_stocks_result = helper.all_stocks()
        # all_stocks_result.save_to_mongo("stocks", replace=True)
        csi300_stocks_result = helper.query_csi300_stocks()
        csi300_stocks_2018_result = helper.query_csi300_stocks(date="2018-12-31")

        now_stocks = csi300_stocks_result.data["code"].tolist()
        stocks_2018 = csi300_stocks_2018_result.data["code"].tolist()
        logger.info("Current csi300 stocks", stocks=now_stocks)
        logger.info("csi300 stocks in 2018", stocks=stocks_2018)
        logger.info(
            "Stocks that were in csi300 in 2018 but not now",
            stocks=list(set(stocks_2018) - set(now_stocks)),
        )
