# 开发者环境

## 安装开发依赖

```bash
poetry install --with dev
```

## 常用开发命令

```bash
poetry run pytest
poetry run pytest -m integration
poetry run pytest tests/agents/derived_facts/test_accounts_receivable_yoy.py
poetry run ruff check .
poetry run mypy alphabee main.py
```

## AI 客户端 skills 目录约定

仓库把可复用 skills 统一维护在 `.ai/skills/`，再通过符号链接暴露给不同客户端：

| 路径 | 用途 |
|------|------|
| `.ai/skills/` | skills 源目录（单一事实来源） |
| `.claude/skills` | Claude Code 使用 |
| `.github/skills` | GitHub Copilot 使用 |
| `.opencode/skills` | OpenCode 使用 |

### Windows 下的 Claude / OpenCode / Copilot skills 连接

Windows 上如果 `git` 没有正确保留 symlink，`.claude/skills`、`.github/skills`、`.opencode/skills` 可能退化为普通文件或缺失。推荐流程：

1. 打开 **Developer Mode**，或使用管理员权限终端。
2. 先启用 Git 的 symlink 支持，再 clone 仓库。
3. clone 完成后执行仓库自带恢复脚本。

PowerShell / Git Bash 示例：

```powershell
git config --global core.symlinks true
git clone https://github.com/captainemo/AlphaBee.git
cd AlphaBee
bash scripts/setup-ai-symlinks.sh
```

脚本会尝试恢复以下链接，且**不会覆盖**已有真实目录或普通文件：

```text
.claude/skills  -> ../.ai/skills
.github/skills  -> ../.ai/skills
.opencode/skills -> ../.ai/skills
```

可用下面的命令确认链接状态：

```powershell
Get-Item .claude\skills, .github\skills, .opencode\skills |
  Select-Object FullName, LinkType, Target
```

如果你只使用 PowerShell，也可以在确认目标路径没有本地改动后手动创建符号链接：

```powershell
New-Item -ItemType SymbolicLink -Path .claude\skills -Target ..\.ai\skills
New-Item -ItemType SymbolicLink -Path .github\skills -Target ..\.ai\skills
New-Item -ItemType SymbolicLink -Path .opencode\skills -Target ..\.ai\skills
```

## 目录结构

```
alphabee/
  agents/
    facts/              FactCollectorAgent — 8 工具 + Pydantic 数据模型
    derived_facts/      DerivedFacts — 21 条 YAML + 拓扑排序引擎
    signal/             SignalEngine — 20 条 YAML（基础风险 + 异常模式）
    anomaly/            AnomalyEngine — 10 条勾稽关系 + z-score
    explore_conflicts/  冲突探索 Agent — 5 大矛盾模式 + 候选假设
    verify_hypotheses/  假设验证 Agent — web_search + Tushare + 研报
    insights/           洞察综合层（整合冲突、验证与信号）
    thesis/             ThesisEngine + Critic + Enhancer + Reviewer
    fact_analysis/      综合分析（占位）
  market_regime/       市场状态引擎（指数估值/趋势/流动性/仓位建议，独立市场级 track）
  orchestrator/         StateGraph 主编排 + 报告生成 / 质量门控
  task_records/         任务记录采集 / 分析 / 蒸馏
  adapters/             Tushare/AkShare 字段映射 (YAML)
  collectors/           数据采集层 (Tushare/AkShare/Baostock)
  config/               配置读取
  core/                 核心 schema (Run/Step/Artifact/Decision/Issue)
  data_fetch/           数据获取管线 (CLI + scanner + database + fingerprint)
  financial_report/     财报 markdown 拆分 (report_parser) + 报告检索代理 (fetch / fetch_deepagents)
  harness/              Harness 提示词资产（作为库被 orchestrator 复用）
  middleware/           Web Search 隔离 / 消息限制
  schemas/              规范字段定义 (INDEX.yaml, 125 字段)
  tools/                通用工具 (web_search, symbol 提取, 本地财报查询 query_financial_report)
  utils/                LLM 客户端 / 日志
  workflow/             监控工作流 (FrameworkMonitor)
.ai/skills/             统一维护的技能目录
.claude/                Claude Code 本地配置
.github/                Copilot 指令与 skills 链接
.opencode/              OpenCode 配置与 skills 链接
main.py                 CLI 入口
config.yaml             运行配置
tests/                  测试套件
```

## 后续工作

### 1. 完善任务记录与规则自蒸馏

**现状**：`task_records/` 模块已实现基础采集（`TaskRecorder` — 包含完整报告 JSON `report_raw`）、存储（`TaskStore`）、确定性分析（`TaskAnalyzer`）和 LLM 蒸馏建议（`RuleDistiller`）。每次运行自动保存记录到 `data/task_records/<symbol>/`，通过 `--task-stats` / `--distill` 产出统计和蒸馏报告。

**后续**：

- **阶段计时采集**：在 StateGraph 节点间注入 timing hook，使 `StageTiming` 数据自动填充（当前依赖手动传参）
- **信号触发率回归检测**：规则修改后自动对比修改前后的触发率变化，检测规则退化
- **蒸馏闭环自动化**：`--distill` 产出的候选 YAML 增加 diff 对比 + 一键回测功能
- **行业基线自建**：积累 100+ 不同行业标的的运行记录后，自动计算行业 μ±σ 作为 reviewer 的对比基准

### 2. 增加上下文压缩

**现状**：FactCollectorAgent 的 LLM 调用和报告生成 LLM 调用都直接消费原始上下文，没有压缩层。当历史记录积累、FactCollector 输出的 raw_response 变长时，context window 压力递增。

**设计方向**：

- **分层压缩**：对 `fact_collection` artifact 的 raw_response 做结构化摘要提取（保留数值表格，压缩叙述文字）
- **角色感知剪裁**：参考旧 harness 的 node-aware slicing 思路，不同节点接收不同粒度的上下文（如 report 生成需要完整数据，thesis 审查只需摘要）
- **增量注入**：多轮对话中，前一轮的完整 report 压缩为结论 + 关键指标快照后再注入下一轮 context

### 3. 增加记忆力模块——用户投资画像

**目标**：记录用户在多次查询中关注的公司、行业、分析维度偏好，逐步构建用户投资画像，使系统能提供更个性化的分析视角和关注点提醒。

**设计方向**：

- **画像维度**：
  - 行业偏好（用户查询频次最高的申万行业）
  - 风格偏好（价值/成长、大盘/中小盘、高分红/高增长）
  - 风控偏好（对确定性要求高/低、对杠杆容忍度、对估值敏感度）
  - 关注维度权重（财务真实性 vs 成长性 vs 估值合理性，用户更关注哪个）
- **采集方式**：
  - 零侵入：从 `TaskRecord` 的 `query` / `symbol` / `flags` 字段累积，不额外询问用户
  - 维度偏好从 `--enhance` 的使用频率和 reviewer issue 分布推断
- **输出**：
  - 报告中加入"与你投资风格的匹配度"视角
  - 多轮对话中主动提示"你上次关注的 XX 行业/公司有新财报"（如启用 Monitor）
  - `--task-stats` 中增加用户画像卡片
- **存储**：`data/user_profile.json`，定期从 `data/task_records/` 重算更新
