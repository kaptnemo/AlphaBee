import os
import re
from typing import Any

import yaml

from alphabee.utils.paths import PROJECT_ROOT


class ConfigLoader:
    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = str(PROJECT_ROOT / "config.yaml")

        self.config_path = config_path

    def _replace_env_variables(self, config_dict: dict[str, Any]) -> dict[str, Any]:
        """替换配置中的环境变量"""
        if isinstance(config_dict, dict):
            return {k: self._replace_env_variables(v) for k, v in config_dict.items()}
        elif isinstance(config_dict, list):
            return [self._replace_env_variables(item) for item in config_dict]
        elif isinstance(config_dict, str):
            # 匹配 ${VAR_NAME:default_value} 格式
            pattern = r"\$\{([^}]+)\}"
            matches = re.findall(pattern, config_dict)

            for match in matches:
                var_parts = match.split(":", 1)
                var_name = var_parts[0]
                default_value = var_parts[1] if len(var_parts) > 1 else None

                env_value = os.getenv(var_name, default_value)
                config_dict = config_dict.replace(f"${{{match}}}", str(env_value))

            return config_dict
        else:
            return config_dict

    def load_config(self) -> dict[str, Any]:
        """加载并处理环境变量"""
        with open(self.config_path, encoding="utf-8") as file:
            config = yaml.safe_load(file)

        config = self._replace_env_variables(config)
        return config

    def get_neo4j_config(self) -> dict[str, Any]:
        """获取 Neo4j 专用配置"""
        config = self.load_config()
        neo4j_config = config.get("neo4j", {})

        return {
            "uri": neo4j_config.get("uri"),
            "auth": (
                neo4j_config.get("auth", {}).get("username"),
                neo4j_config.get("auth", {}).get("password"),
            ),
            "database": neo4j_config.get("database"),
            "encrypted": neo4j_config.get("encrypted", False),
        }
