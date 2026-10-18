# 配置说明

从 `config.yaml.example` 复制为 `config.yaml`，支持 `${ENV_VAR}` 和 `${ENV_VAR:default}` 占位符；`.env` 中的变量会在启动时自动加载：

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://api.deepseek.com}"
  model: "${LLM_MODEL:deepseek-chat}"

web_search:
  tavily:
    api_key: "${TAVILY_API_KEY:}"
  ddgs:
    region: "cn-zh"

tushare:
  api_key: "${TUSHARE_TOKEN:}"

data:
  root_dir: "${DATA_ROOT:data}"
```

## 环境变量

| 环境变量 | 说明 | 必须 |
|----------|------|------|
| `LLM_API_KEY` | 大模型 API 密钥 | ✅ |
| `LLM_BASE_URL` | 大模型 API 地址（默认 `https://api.deepseek.com`） | 可选 |
| `LLM_MODEL` | 模型名称（默认 `deepseek-chat`） | 可选 |
| `TUSHARE_TOKEN` | Tushare 数据 Token | 建议填写 |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| `LANGFUSE_ENABLE` | 是否启用 Langfuse 追踪（默认 `false`） | 可选 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 可观测性 | 可选 |
| `LANGFUSE_BASE_URL` | Langfuse 服务地址（默认 `http://localhost:3000`） | 可选 |
| `DATA_ROOT` | 产物根目录（默认 `data/`） | 可选 |

## 偏离控制框架（deviation）

`deviation.tracking` 段承载宏观环跟踪（tracking）的阈值与开关（P0 设计 §7.1）。缺段时全部取内置默认值，旧配置仍可 import：

```yaml
deviation:
  tracking:
    price_move_mode: absolute   # 行情异动阈值口径（absolute | limit_relative）
    price_move_ratio: 0.5       # limit_relative 触发比例（|change|/board_limit ≥ 0.5）
```

- `price_move_mode: absolute`（默认）= 现状绝对值口径：`|price_change_pct| > 5.0%` 触发。
- `price_move_mode: limit_relative` = 按板块涨跌停幅度归一：`|change|/board_limit ≥ price_move_ratio`
  即触发，且封板边界 `|change| ≥ board_limit − 0.5pp` 也触发（覆盖 ST ≈4.95% 与涨停 9.99% 漏判）。
  `price_move_ratio: 0.5` ⇒ 主板 5%、双创 10%、北交所 15%，与主板绝对口径 `5.0%` 一致。

`main.py` 会自动加载项目根目录下的 `.env`，配置文件则从 `config.yaml` 读取。
