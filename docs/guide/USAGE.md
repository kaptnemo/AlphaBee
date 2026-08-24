# 使用指南

## 常用运行方式

```bash
# 单次分析
poetry run python main.py "帮我分析一下宁德时代的投资价值"

# 多轮对话
poetry run python main.py --chat

# 启用 LLM 增强层（跨信号模式 + 行业语境化）
poetry run python main.py --enhance "分析 600519.SH"

# 全开：增强层 + LLM 审查
poetry run python main.py --enhance --llm-review "分析比亚迪"

# 关闭颜色输出
poetry run python main.py --no-color "分析 000858.SZ"

# 指定日志目录
poetry run python main.py --log-dir ./my_logs "分析宁德时代"
```

## 多轮对话命令

| 命令 | 说明 |
|------|------|
| 直接输入问题 | 继续追问 |
| `/clear` | 清空上下文 |
| `/exit` | 退出会话 |

## 框架监控模式

```bash
# 基于预定义监控框架持续评估特定标的
poetry run python main.py --monitor-framework monitor_framework.md --symbol 300760.SZ

# 指定监控期数
poetry run python main.py --monitor-framework monitor_framework.md --symbol 300760.SZ --monitor-periods 12
```

监控模式读取 Markdown 格式的监控框架文件，对指定标的拉取最新多期财务数据，按框架论点逐条评估并生成结构化监控报告。

## 任务记录与分析

每次运行自动保存记录到 `data/task_records/<symbol>/`。

```bash
# 查看统计摘要
poetry run python main.py --task-stats

# 生成规则蒸馏建议报告（需 LLM）
poetry run python main.py --distill

# 查看指定标的的历史运行记录
poetry run python main.py --task-history 600519.SH

# 查看单次运行的完整记录
poetry run python main.py --task-record task-a1b2c3d4e5f6
```
