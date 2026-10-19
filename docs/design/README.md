# 分析设计文档

> 本目录存放**整体分析与设计文档**：回答"为什么/是什么/机制怎么设计"的文档——框架评审、架构设计、机制设计、缺陷分析、研究设计。
> 具体实施文档（怎么改代码）在 `docs/implementation/`。

## 分类规则

| 目录 | 放什么 | 判据 |
|---|---|---|
| `docs/design/`（本目录） | 整体分析与设计：评审、框架/机制设计、缺陷分析、研究设计 | 回答"为什么/是什么/怎么设计"，不含代码级施工细节 |
| `docs/implementation/` | 具体实施：修复计划、编码设计、实施设计 | 含文件清单/签名/接线锚点/测试矩阵/PR 切分/回滚等施工细节 |

**判定冲突时的优先级**：含代码级施工细节（文件清单、函数签名、接线锚点、测试矩阵、PR 切分、回滚方案中 ≥3 项）→ `docs/implementation/`；否则 → 本目录。

## 当前文档

- 框架级评审与设计：`A_SHARE_FRAMEWORK_REVIEW.md`、`RESEARCH_CONTINUUM_DESIGN.md`、`DEVIATION_CONTROL_FRAMEWORK.md`、`INVESTMENT_HARNESS_DESIGN.md`
- 机制设计：`INSIGHT_DEGRADATION_DESIGN.md`、`THESIS_CONFIDENCE_DAMPING_DESIGN.md`、`SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`、`MIDTERM_INSIGHT_INJECTION_DESIGN.md`、`PEER_GROUP_JUDGMENT_DESIGN.md`、`RULE_MARKET_ADAPTATION_DESIGN.md`、`data_collection_failed_auto_fix_design.md`
- 分析与研究设计：`THESIS_CONTEXT_MISMATCH_DEFECT_ANALYSIS.md`、`BEAT_MISS_LR_RESEARCH_DESIGN.md`、`DRIVER_PROFILE_RESEARCH_DESIGN.md`、`REVIEW_REGIME_DOMAIN_CONTEXT.md`（评审笔记，自 `docs/roadmap/` 迁入）
