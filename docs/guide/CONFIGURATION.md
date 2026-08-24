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

`main.py` 会自动加载项目根目录下的 `.env`，配置文件则从 `config.yaml` 读取。
