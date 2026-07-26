from pathlib import Path

import yaml
from pandas import DataFrame

ADAPTER_CONFIG_DIR = Path(__file__).parent / "akshare"


class AkShareAdapter:
    def __init__(self):
        self.adapter_config = self.load_adapter_config()

    def load_adapter_config(self) -> dict:
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
        if method_name not in self.adapter_config:
            return df_data

        adapter_columns = dict(self.adapter_config[method_name])
        return df_data.rename(columns=adapter_columns, errors="ignore")


AkShare_Adapter = AkShareAdapter()
