"""TaskAnalyzer — 统计分析与模式发现（纯确定性，不调用 LLM）。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from alphabee.task_records.store import TaskStore


class TaskAnalyzer:
    """对 TaskStore 中的记录做统计分析。

    用法::

        store = TaskStore()
        analyzer = TaskAnalyzer(store)
        print(analyzer.signal_trigger_rates())
        print(analyzer.issue_frequency())
    """

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    # ── 执行问题分析 ──────────────────────────────────────────────

    def run_count(self) -> int:
        """总运行次数。"""
        return self.store.count()

    def issue_frequency(self, limit: int = 15) -> list[tuple[str, int]]:
        """最高频的 issue category（如 thesis_gap, missing_data 等）。"""
        counter: Counter[str] = Counter()
        for r in self.store.load_all():
            for i in r.issues:
                counter[i.category] += 1
        return counter.most_common(limit)

    def issue_message_clusters(
        self, keywords: list[str] | None = None, top_n: int = 20,
    ) -> list[tuple[str, int]]:
        """按关键词聚类 issue message。

        Args:
            keywords: 自定义关键词列表（None 则用内置列表）。
            top_n: 返回前 N 条最高频。
        """
        if keywords is None:
            keywords = [
                "语境不适配", "行业信息不足", "证据单薄",
                "信号方向冲突", "thesis 判断", "结构调整",
                "缺乏行业对比", "缺少行业负债率基准",
                "单证据", "映射错位",
            ]
        counter: Counter[str] = Counter()
        for r in self.store.load_all():
            for i in r.issues:
                for kw in keywords:
                    if kw in i.message:
                        counter[kw] += 1
                        break
        return counter.most_common(top_n)

    def stage_timing_stats(self) -> dict[str, Any]:
        """各阶段耗时统计（平均/中位数/最大）。"""
        stages: dict[str, list[float]] = {}
        for r in self.store.load_all():
            for st in r.stage_timings:
                stages.setdefault(st.stage, []).append(st.elapsed_s)
        result: dict[str, Any] = {}
        for stage, times in stages.items():
            t_sorted = sorted(times)
            result[stage] = {
                "count": len(t_sorted),
                "avg": round(sum(t_sorted) / len(t_sorted), 2),
                "median": round(t_sorted[len(t_sorted) // 2], 2),
                "max": round(t_sorted[-1], 2),
            }
        return result

    def flag_impact(self) -> dict[str, Any]:
        """--enhance / --llm-review 对 overall_confidence 的影响。"""
        groups: dict[str, list[str]] = {
            "default": [],
            "enhance_only": [],
            "llm_review_only": [],
            "both": [],
        }
        for r in self.store.load_all():
            enhance = r.flags.get("enhance", False)
            llm = r.flags.get("llm_review", False)
            if enhance and llm:
                groups["both"].append(r.overall_confidence)
            elif enhance:
                groups["enhance_only"].append(r.overall_confidence)
            elif llm:
                groups["llm_review_only"].append(r.overall_confidence)
            else:
                groups["default"].append(r.overall_confidence)

        result: dict[str, Any] = {}
        for key, confidences in groups.items():
            if not confidences:
                result[key] = {"count": 0}
                continue
            counter = Counter(confidences)
            result[key] = {
                "count": len(confidences),
                "high_pct": round(counter.get("high", 0) / len(confidences) * 100, 1),
                "medium_pct": round(counter.get("medium", 0) / len(confidences) * 100, 1),
                "low_pct": round(counter.get("low", 0) / len(confidences) * 100, 1),
            }
        return result

    def avg_duration(self) -> float:
        """平均耗时（秒）。"""
        records = self.store.load_all()
        if not records:
            return 0.0
        return round(sum(r.total_duration_s for r in records) / len(records), 1)

    # ── 规则覆盖分析 ──────────────────────────────────────────────

    def signal_trigger_rates(self) -> dict[str, dict[str, Any]]:
        """每条信号规则的触发率统计。"""
        total = 0
        counts: dict[str, Counter[str]] = {}
        for r in self.store.load_all():
            total += 1
            for s in r.signal_results:
                if s.signal_id not in counts:
                    counts[s.signal_id] = Counter()
                counts[s.signal_id][s.level] += 1

        result: dict[str, dict[str, Any]] = {}
        for sid, counter in counts.items():
            runs = sum(counter.values())
            result[sid] = {
                "runs": runs,
                "triggered_pct": round(
                    (counter.get("high", 0) + counter.get("medium", 0) + counter.get("low", 0))
                    / runs * 100, 1
                ) if runs else 0,
                "high_pct": round(counter.get("high", 0) / runs * 100, 1) if runs else 0,
                "medium_pct": round(counter.get("medium", 0) / runs * 100, 1) if runs else 0,
                "low_pct": round(counter.get("low", 0) / runs * 100, 1) if runs else 0,
                "blocked_pct": round(counter.get("blocked", 0) / runs * 100, 1) if runs else 0,
            }
        return dict(sorted(result.items()))

    def single_evidence_dimensions(self) -> list[tuple[str, int]]:
        """哪些维度最常被 reviewer 标记'证据单薄'。"""
        counter: Counter[str] = Counter()
        for r in self.store.load_all():
            for i in r.issues:
                if "证据单薄" in i.message or "一条信号支撑" in i.message:
                    dim = self._extract_dim_from_message(i.message)
                    if dim:
                        counter[dim] += 1
        return counter.most_common()

    def anomaly_pattern_frequencies(self) -> list[tuple[str, int]]:
        """每个异常模式的触发频率。"""
        counter: Counter[str] = Counter()
        for r in self.store.load_all():
            for a in r.anomaly_details:
                for pid in a.pattern_ids:
                    counter[pid] += 1
        return counter.most_common()

    def high_zscore_rules(
        self, min_runs: int = 3, z_threshold: float = 2.5,
    ) -> list[dict[str, Any]]:
        """哪些勾稽关系规则最常触发高 z-score。"""
        counter: Counter[str] = Counter()
        runs: Counter[str] = Counter()
        for r in self.store.load_all():
            for a in r.anomaly_details:
                runs[a.rule_id] += 1
                if abs(a.z_score) >= z_threshold:
                    counter[a.rule_id] += 1
        return sorted(
            [
                {"rule_id": rid, "high_z_runs": count, "total_runs": runs.get(rid, 0),
                 "high_z_rate": round(count / runs.get(rid, 1) * 100, 1)}
                for rid, count in counter.items()
                if runs.get(rid, 0) >= min_runs
            ],
            key=lambda x: x["high_z_rate"],
            reverse=True,
        )

    # ── 问题模式 ──────────────────────────────────────────────────

    def context_gap_industries(self) -> list[tuple[str, int]]:
        """哪些行业最常出现'语境不适配' issue。"""
        counter: Counter[str] = Counter()
        for r in self.store.load_all():
            has_context_issue = any(
                "语境不适配" in i.message or "行业信息不足" in i.message
                for i in r.issues
            )
            if has_context_issue and r.company_industry:
                counter[r.company_industry] += 1
        return counter.most_common()

    def contested_dimension_frequency(self) -> list[tuple[str, int]]:
        """各维度被标记为 contested 的频率。"""
        counter: Counter[str] = Counter()
        total: Counter[str] = Counter()
        for r in self.store.load_all():
            for d in r.review_dimension_verdicts:
                total[d.dim_name or d.dim_id] += 1
                if d.status == "contested":
                    counter[d.dim_name or d.dim_id] += 1
        return sorted(
            [
                (dim, cnt) for dim, cnt in counter.items()
            ],
            key=lambda x: x[1],
            reverse=True,
        )

    def blocked_dimension_rate(self) -> list[dict[str, Any]]:
        """各维度被 blocked 的比例。"""
        counter: Counter[str] = Counter()
        total: Counter[str] = Counter()
        for r in self.store.load_all():
            for d in r.review_dimension_verdicts:
                dim = d.dim_name or d.dim_id
                total[dim] += 1
                if d.status in ("contested", "insufficient"):
                    counter[dim] += 1
        return sorted(
            [
                {"dimension": dim, "blocked_pct": round(counter[dim] / total[dim] * 100, 1)}
                for dim in total
            ],
            key=lambda x: x["blocked_pct"],
            reverse=True,
        )

    # ── 综合摘要 ──────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """一键产出综合统计摘要。"""
        return {
            "run_count": self.run_count(),
            "avg_duration_s": self.avg_duration(),
            "top_issues": self.issue_frequency(10),
            "top_message_clusters": self.issue_message_clusters(top_n=10),
            "flag_impact": self.flag_impact(),
            "signal_trigger_rates": self.signal_trigger_rates(),
            "single_evidence_dims": self.single_evidence_dimensions(),
            "anomaly_patterns": self.anomaly_pattern_frequencies(),
            "context_gap_industries": self.context_gap_industries(),
            "contested_dims": self.contested_dimension_frequency(),
        }

    # ── 工具方法 ──────────────────────────────────────────────────

    @staticmethod
    def _extract_dim_from_message(message: str) -> str:
        """从 issue message 中提取维度名（如 '[盈利质量]'）。"""
        if message.startswith("[") and "]" in message:
            end = message.index("]")
            return message[1:end]
        return ""
