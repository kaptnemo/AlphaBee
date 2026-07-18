from pathlib import Path

import yaml
from pandas import DataFrame

ADAPTER_CONFIG_DIR = Path(__file__).parent / "tushare"


class TuShareAdapter:
    def __init__(self):

        self.adapter_config = self.load_adapter_config()

    def load_adapter_config(self) -> dict:
        """Load adapter configuration from a file."""
        adapter_config = {}
        for root, dirs, files in ADAPTER_CONFIG_DIR.walk():
            for file in files:
                if file.endswith(".yaml"):
                    config_path = Path(root) / file
                    with open(config_path, encoding="utf-8") as f:
                        config = yaml.safe_load(f)
                        for key, value in config.items():
                            adapter_config[key] = value
        return adapter_config

    def adapt(self, method_name: str, df_data: DataFrame) -> DataFrame:
        """Rename DataFrame columns from Tushare field names to AlphaBee canonical names."""
        if method_name not in self.adapter_config:
            return df_data

        # Create a copy to avoid mutating the shared config dict
        adapter_columns = {**self.adapter_config[method_name], "ts_code": "stock_code"}

        return df_data.rename(columns=adapter_columns, errors="ignore")


TuShare_Adapter = TuShareAdapter()
