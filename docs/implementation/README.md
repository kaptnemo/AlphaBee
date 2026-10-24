# 实施设计文档

> 本目录存放**具体实施文档**：回答"怎么改代码"的文档——含文件清单、类/函数签名、接线点（既有代码锚点）、序列化与兼容策略、测试矩阵、PR 切分与回滚、验收标准。
> 与 `docs/design/`（整体分析与设计：回答"为什么/是什么/机制怎么设计"）的边界见 `docs/design/README.md`。
> **本目录按代码模块再分一层**（子目录名 = `alphabee/` 下的模块名）：

## 子目录结构

| 子目录 | 对应代码模块 | 文档 | 状态 |
|---|---|---|---|
| `framework/` | 跨模块（midterm + tracking + market_regime + domain_context） | `P0_A_SHARE_FIX_IMPLEMENTATION_DESIGN.md`（框架评审 P0 六项修复） | ✅ 已实施（PR-A/B/C） |
| `midterm/` | `alphabee/midterm/`（+ `collectors/consensus`） | `MIDTERM_CLASSIFIER_TEF_DESIGN.md`（T→E→F 分类器变体） | ✅ 已实施（PR-1/2），A/B 实验待执行 |
| | | `E_ECOSYSTEM_FIX_DESIGN.md`（E 数据生态：预告类型加权 + 保守锚 + 东财盈利预测） | ⬜ 未实施 |
| `thesis/` | `alphabee/agents/thesis/` | `THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`（论点语境不适配修复） | ✅ 已实施 |
| `company_track/` | `alphabee/company_track/` | `TRACK_LABEL_COARSE_BUCKET_FIX_PLAN.md`（赛道标签粗口径单桶退化修复） | ✅ 已实施 |
| `domain_context/` | `alphabee/domain_context/`（驱动画像 DRIVER_PROFILE） | `DRIVER_PROFILE_D0_CODING_DESIGN.md` / `DRIVER_PROFILE_D1_D2_CODING_DESIGN.md`（编码设计） | ✅ 已实施 |
| | | `DRIVER_PROFILE_P0_FIX_PLAN.md` / `DRIVER_PROFILE_P1_P2_FIX_PLAN.md`（修复计划） | ✅ 已实施 |
| `financial_report/` | `alphabee/financial_report/`（+ `tools/financial_report.py`） | `REPORT_INDEX_AGENT_CACHE_DESIGN.md`（财报检索 index / fetch agent 缓存 + 路径守卫） | ✅ 已实施（P0/P1/P2），P3 未实施 |

## 分类规则

文档同时满足以下两条的**实施文档**放本目录：

1. 面向**代码改动落地**（文件名或内容含 FIX_PLAN / CODING_DESIGN / IMPLEMENTATION / 实施设计等）；
2. 含**代码级施工细节**：文件清单、函数签名、接线锚点、测试矩阵、PR 切分、回滚方案中至少三项。

子目录归属按**主要触及的代码模块**；跨多模块的实施（如 P0 六项）放 `framework/`。
