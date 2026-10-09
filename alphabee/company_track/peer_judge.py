"""对标组候选的**生产侧调用入口**（判定 C 结构化维度 / D 独立 judge）。

本模块承载两类 LLM 调用与全部 prompt 构造，供**在线链路（后续接入）**与**离线评测 harness**
（``scripts/peer_group_eval.py``）共用同一份 prompt 与解析逻辑，消除“harness 内再抄一份
prompt”的双维护（设计 §5.2）。

- :func:`infer_peer_scoring` —— 生成器视角：对**给定候选池**逐条给 ``overlap`` + 结构化维度（C）；
- :func:`judge_peer_candidates` —— 独立评审视角（单批）：对**给定候选池**逐条给 ``verdict`` + 维度（D）；
- :func:`judge_peer_candidates_batched` —— 独立评审视角（**生产入口**）：按 ``batch_size`` 切批调用，
  以 :class:`JudgeReport` 显式给出「可采纳 / 降级」与降级原因（fail-open 的**显式**载体，设计 §3.6）。

契约（生成器与 judge 一致，均可缓存、可离线复算）：

- 入参候选形态为 ``{code, name, same_l3?, same_l2?}``（``same_*`` 为分类学特征，缺省不注入）；
- 出参为 ``code → 结果 dict``（键统一大写去空格）；**未见过的 code 一律丢弃**（防漂移）；
- ``dims`` 恒为四维（``product/customer/material_tech/business_model``）并归一到 ``[0, 1]``；
- ``verdict`` 仅取 ``direct | adjacent | reject``，非法值 → ``None``（视为未判定）；
- **fail-open**：无描述 / 无候选 / LLM 异常 / 输出非 JSON 数组 → 不抛异常；批量入口返回
  ``JudgeReport.ok = False``（``degraded = True``），由调用方**回退生成器分**继续走 Gate，
  **绝不**据此把对标组判成 ``no_peers``。

**在线接线（设计 §6 Step 2）**：``peer_group_build.build_peer_group`` 在
``company_track.peer_quality.judge_enabled`` 时对生成器候选池调用
:func:`judge_peer_candidates_batched`，把 judge 的 ``verdict`` / ``dims`` 交给 Gate（判定 D）；
judge 降级时回退生成器的 ``dims`` / ``overlap``（判定 C 口径）并在 notes 记 ``judge_degraded``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "DIMS",
    "JUDGE_BATCH_SIZE_DEFAULT",
    "JudgeReport",
    "MAX_DESCRIPTION_CHARS",
    "MODEL_COMPONENT",
    "VALID_VERDICTS",
    "build_judge_prompt",
    "build_scoring_prompt",
    "coerce_dims",
    "coerce_overlap",
    "judge_peer_candidates",
    "judge_peer_candidates_batched",
    "infer_peer_scoring",
    "resolve_peer_eval_model",
]

#: 与生产生成器同组件（设计 §8 决策 1：judge 与生成器同模型，不新增模型配置项）。
MODEL_COMPONENT = "agent.peer_group"

#: 结构化匹配维度（判定 C）：产品/服务、客户/终端、材料/技术路线、盈利模式/业态。
DIMS: tuple[str, ...] = ("product", "customer", "material_tech", "business_model")

#: judge 的 verdict 三级语义（判定 D，设计 §3.2）。
VALID_VERDICTS: frozenset[str] = frozenset({"direct", "adjacent", "reject"})

#: judge 单批候选数上限（设计 §3.5；``company_track.peer_quality.judge_batch_size`` 覆盖）。
JUDGE_BATCH_SIZE_DEFAULT = 20

#: 业务描述入 prompt 的字符上限（控制 token；与生产生成器一致）。
MAX_DESCRIPTION_CHARS = 8000


class ChatModel(Protocol):
    """最小模型契约（``langchain`` ChatModel 的 ``invoke`` 子集），便于注入假模型。"""

    def invoke(self, prompt: str) -> Any: ...


# ── 归一化 ──────────────────────────────────────────────────────────
def coerce_overlap(value: Any) -> float | None:
    """``overlap`` 归一到 ``[0, 1]``；缺失/非法/NaN → ``None``（不因此剔除）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number > 1.0:  # 容忍 LLM 用百分数（85 → 0.85）
        number = number / 100.0
    return max(0.0, min(1.0, number))


def coerce_dims(raw: Any) -> dict[str, float] | None:
    """``dims`` 归一到**四维齐全**的 ``[0, 1]`` 浮点表；非 dict → ``None``。

    缺字段 / 非法值（``"高"`` / ``None``）→ 该维 ``0.0``；多余维度忽略。
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for dim in DIMS:
        value = raw.get(dim)
        if value is None:
            out[dim] = 0.0
            continue
        try:
            out[dim] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            out[dim] = 0.0
    return out


def _coerce_verdict(raw: Any) -> str | None:
    """``verdict`` 三级语义校验；大小写/空格容错，非法值 → ``None``。"""
    text = str(raw or "").strip().lower()
    return text if text in VALID_VERDICTS else None


def _coerce_code(raw: Any) -> str:
    return str(raw or "").strip().upper()


# ── 候选池渲染 / prompt 构造（唯一处：harness 不再另写 prompt） ──────
def _candidates_block(candidates: list[dict[str, Any]]) -> str:
    """候选清单渲染为 ``- 600584.SH 长电科技（same_l3=False，same_l2=True）``。

    ``same_l3`` / ``same_l2`` 为 ``None``（无分类学数据）时不写该标签（设计 §3.6：judge 忽略）。
    """
    lines: list[str] = []
    for item in candidates:
        code = _coerce_code(item.get("code"))
        name = str(item.get("name") or "").strip()
        tags: list[str] = []
        for key in ("same_l3", "same_l2"):
            hint = item.get(key)
            if hint is not None:
                tags.append(f"{key}={bool(hint)}")
        suffix = f"（{'，'.join(tags)}）" if tags else ""
        lines.append(f"- {code} {name}{suffix}")
    return "\n".join(lines)


def build_scoring_prompt(
    business_description: str,
    candidates: list[dict[str, Any]],
    *,
    industry: str = "",
) -> str:
    """生成器打分 prompt（判定 C）：对**给定候选**逐条给 ``overlap`` + 四维分。"""
    return (
        "你是买方研究员。给定标的主要业务与候选公司清单，对**每个候选**判断其与标的在"
        "以下维度的重叠度（0–1）：product(产品/服务)、customer(客户/终端应用)、"
        "material_tech(材料/技术路线)、business_model(盈利模式/业态)，并给出 overall overlap（0–1）。\n"
        "候选清单已给定，不要新增或改动代码。\n"
        "只输出 JSON 数组（无候选则 []），每条："
        '{"code": "...", "overlap": 0.0-1.0, "dims": {"product":0,"customer":0,"material_tech":0,"business_model":0}, "reason": "一句理由"}。\n'
        f"行业: {industry or '未标注'}\n"
        f"候选清单:\n{_candidates_block(candidates)}\n\n"
        f"标的主要业务描述:\n{business_description[:MAX_DESCRIPTION_CHARS]}"
    )


def build_judge_prompt(
    business_description: str,
    candidates: list[dict[str, Any]],
) -> str:
    """独立 judge prompt（判定 D）：先列匹配点/不匹配点，再给 ``verdict`` + 四维分。

    硬约束（设计 §3.2；跨行业通用、不含任何行业专属词）：

    1. **reasoning-first**：``reason`` 必须先写「匹配点」与「不匹配点」，再据此给 ``verdict`` 与 ``dims``；
    2. ``verdict`` 三级语义明确（同环节可互为替代 / 相邻部分重叠 / 不同环节或实质不同）；
    3. **同 L3 是强证据但不能替代业务判断**（L3 不同而产品/客户高度重叠仍可 ``direct``；
       同 L3 而材料/终端/盈利模式不同应 ``reject``）；
    4. **闭集**：只能对**给定候选清单内**的代码给结论，不得新增、改动或猜测代码；
    5. 不要求 LLM 做采纳决定（决定权交下游确定性 Gate）。
    """
    return (
        "你是**独立评审**（与生成者无关）。给定标的主要业务、候选公司清单及其与标的的申万分类关系，"
        "判断每个候选是否为**同产业链环节的直接对标**：\n"
        '- verdict: "direct"（同环节、可互为替代）| "adjacent"（相邻/部分重叠）| "reject"（不同环节或材料/终端实质不同）；\n'
        "- dims: product/customer/material_tech/business_model（0–1）；\n"
        "- reason: **先列「匹配点」与「不匹配点」**，再据此给 verdict 与 dims（不得先给分数再补理由）。\n"
        "规则：同 L3 是「同环节」的强证据，但**不能替代业务判断**——L3 不同但产品/客户高度重叠仍应 direct；"
        "同 L3 但材料/终端/盈利模式不同应 reject。\n"
        "你只需给出 verdict 与维度分，**不负责决定是否采纳**（是否入选由下游确定性闸门决定）。\n"
        "候选清单已给定：**只能对清单内的代码给出结论**，不得新增、改动或猜测代码（清单外的代码一律忽略）。\n"
        "只输出 JSON 数组，每条："
        '{"code": "...", "verdict": "direct|adjacent|reject", "dims": {"product":0,"customer":0,"material_tech":0,"business_model":0}, "reason": "..."}。\n'
        f"候选清单（含申万分类关系）:\n{_candidates_block(candidates)}\n\n"
        f"标的主要业务描述:\n{business_description[:MAX_DESCRIPTION_CHARS]}"
    )


# ── 模型解析 ────────────────────────────────────────────────────────
def resolve_peer_eval_model() -> Any:
    """生产同组件 langchain ChatModel（``agent.peer_group``）；仅显式调用时构造，避免 import 期副作用。"""
    from alphabee.utils.llm import create_chat_model

    return create_chat_model(MODEL_COMPONENT)


# ── 调用 + 解析（fail-open） ────────────────────────────────────────
def _invoke_json_array_ex(prompt: str, model: ChatModel | None) -> tuple[list[Any] | None, str]:
    """调模型并解析为 JSON 数组；**不抛异常**。

    Returns:
        ``(数组, "")``：模型返回了可解析的 JSON 数组；
        ``(None, 原因)``：模型调用异常 / 解析失败 / 输出不是 JSON 数组（原因用于 ``judge_degraded`` 记账）。
    """
    try:
        from alphabee.utils.pipeline import parse_json

        active = model if model is not None else resolve_peer_eval_model()
        raw = str(active.invoke(prompt).content)
    except Exception as exc:  # noqa: BLE001 —— fail-open：任何调用异常都只记原因
        return None, f"{type(exc).__name__}: {exc}"
    try:
        parsed = parse_json(raw)
    except Exception as exc:  # noqa: BLE001 —— fail-open：解析失败只记原因
        return None, f"解析失败 {type(exc).__name__}: {exc}"
    if not isinstance(parsed, list):
        return None, "LLM 输出非 JSON 数组"
    return parsed, ""


def _invoke_json_array(prompt: str, model: ChatModel | None) -> list[Any]:
    """``_invoke_json_array_ex`` 的兼容包装：失败 → 空列表（绝不抛）。"""
    payload, _ = _invoke_json_array_ex(prompt, model)
    return payload if payload is not None else []


def _seen_codes(candidates: list[dict[str, Any]]) -> set[str]:
    return {code for code in (_coerce_code(item.get("code")) for item in candidates) if code}


def _ordered_pool(candidates: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """候选池 → ``(去重保序代码列表, code → 原始候选 dict)``（同码重复取首条）。"""
    ordered: list[str] = []
    by_code: dict[str, dict[str, Any]] = {}
    for item in candidates:
        code = _coerce_code(item.get("code"))
        if not code or code in by_code:
            continue
        ordered.append(code)
        by_code[code] = item
    return ordered, by_code


def _collect_judge_rows(
    items: list[Any] | None, allowed: set[str], out: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """把一批解析结果并入 ``out``（**闭集校验**：清单外代码一律丢弃）。

    Returns:
        ``(本批写入的代码, 闭集外被丢弃的代码, 结构非法而被丢弃的代码)``。
    """
    written: list[str] = []
    out_of_set: list[str] = []
    malformed: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            malformed.append(str(item)[:40])
            continue
        code = _coerce_code(item.get("code"))
        if not code:
            malformed.append("<无 code>")
            continue
        if code not in allowed:
            out_of_set.append(code)
            continue
        if code in out:  # 首条优先（同码重复不覆盖）
            continue
        out[code] = {
            "verdict": _coerce_verdict(item.get("verdict")),
            "dims": coerce_dims(item.get("dims")),
            "reason": str(item.get("reason") or "").strip(),
        }
        written.append(code)
    return written, out_of_set, malformed


def _judge_row_valid(row: dict[str, Any] | None) -> bool:
    """一行 judge 结果是否**可用**：verdict 合法三级 + dims 为四维表（缺一即视为未判定）。"""
    if not isinstance(row, dict):
        return False
    return row.get("verdict") in VALID_VERDICTS and isinstance(row.get("dims"), dict)


@dataclass(frozen=True)
class JudgeReport:
    """一次**批量独立评审**的结果与降级记账（fail-open 的显式载体，设计 §3.6）。

    判定口径：

    - :attr:`ok`（**可采纳**）：真的发起了调用、全部批次都返回了可解析的 JSON 数组、
      且候选池**逐只都有可用判定**（verdict 合法 + dims 齐全）。只有 ``ok=True`` 才能把
      judge 的 ``verdict`` / ``dims`` 交给 Gate；
    - ``ok=False`` 且 :attr:`called`（**降级**）：LLM 异常 / 非 JSON / 批次异常 / 候选漏判 ⇒
      调用方**回退生成器分**继续走 Gate（判定 C 口径），记 ``judge_degraded``，
      **绝不置** ``no_peers``（可重试）；
    - ``called=False``（空池 / 无描述）：既未判定也无降级，调用方按生成器口径处理。

    字段（全部为**确定性记账**，便于 notes 与测试断言）：

    - :attr:`results`：``code → {verdict, dims, reason}``（仅候选池内代码）；
    - :attr:`batches` / :attr:`failed_batches`：计划批次数 / 失败批次数；
    - :attr:`missing_codes`：池内**未取得可用判定**的代码（漏判或判定非法）；
    - :attr:`out_of_set_codes`：模型给出但**不在候选池内**的代码（闭集外，已丢弃）；
    - :attr:`errors`：逐批失败原因（``batch#i: ...``）。
    """

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: int = 0
    failed_batches: int = 0
    missing_codes: tuple[str, ...] = ()
    out_of_set_codes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    called: bool = False

    @property
    def ok(self) -> bool:
        """是否可采纳（见类 docstring 的三条判据）。"""
        return self.called and self.failed_batches == 0 and not self.missing_codes

    @property
    def degraded(self) -> bool:
        """是否降级（应该判却没判成 ⇒ 必须回退生成器分，且**不得**置 ``no_peers``）。"""
        return self.called and not self.ok

    def note(self) -> str:
        """降级/异常 → 一行人类可读 notes（``judge_degraded`` 记账的唯一实现处）。"""
        if self.ok:
            return ""
        if not self.called:
            return "judge 未调用（无候选池或无业务描述）"
        parts = [
            f"judge 降级（回退生成器分，不置 no_peers；批次 {self.batches - self.failed_batches}/{self.batches} 有效）"
        ]
        if self.missing_codes:
            parts.append(f"未获可用判定 {len(self.missing_codes)} 只: {','.join(self.missing_codes[:5])}")
        if self.errors:
            parts.append(f"失败原因: {'; '.join(self.errors[:3])}")
        return "，".join(parts)


def infer_peer_scoring(
    business_description: str,
    candidates: list[dict[str, Any]],
    *,
    industry: str = "",
    model: ChatModel | None = None,
) -> dict[str, dict[str, Any]]:
    """对给定候选池逐条产出 ``{overlap, dims, reason}``（判定 C 的生成器侧打分）。

    Returns:
        ``code → {"overlap": float | None, "dims": dict[str, float] | None, "reason": str}``；
        空入参 / 异常 / 非数组 → ``{}``。
    """
    allowed = _seen_codes(candidates)
    if not allowed or not str(business_description or "").strip():
        return {}
    prompt = build_scoring_prompt(business_description, candidates, industry=industry)
    out: dict[str, dict[str, Any]] = {}
    for item in _invoke_json_array(prompt, model):
        if not isinstance(item, dict):
            continue
        code = _coerce_code(item.get("code"))
        if code not in allowed or code in out:
            continue
        out[code] = {
            "overlap": coerce_overlap(item.get("overlap")),
            "dims": coerce_dims(item.get("dims")),
            "reason": str(item.get("reason") or "").strip(),
        }
    return out


def judge_peer_candidates(
    business_description: str,
    candidates: list[dict[str, Any]],
    *,
    model: ChatModel | None = None,
) -> dict[str, dict[str, Any]]:
    """独立 judge（判定 D，**单批**）：对给定候选池逐条产出 ``{verdict, dims, reason}``。

    与生成器**解耦**：输入只含标的业务描述 + 候选清单（含 ``same_l3/same_l2`` 特征），
    **不含生成器分数**，避免自评自利（设计 D-2）。生产在线路径请用
    :func:`judge_peer_candidates_batched`（按批切分 + 降级记账）；本函数是它的**单批原语**，
    也是离线复算/单测的稳定入口。

    Returns:
        ``code → {"verdict": str | None, "dims": dict[str, float] | None, "reason": str}``；
        空入参 / 异常 / 非数组 → ``{}``（fail-open，不置 ``no_peers`` 的语义由调用方承载）。
    """
    allowed = _seen_codes(candidates)
    if not allowed or not str(business_description or "").strip():
        return {}
    prompt = build_judge_prompt(business_description, candidates)
    out: dict[str, dict[str, Any]] = {}
    _collect_judge_rows(_invoke_json_array(prompt, model), allowed, out)
    return out


def judge_peer_candidates_batched(
    business_description: str,
    candidates: list[dict[str, Any]],
    *,
    batch_size: int = JUDGE_BATCH_SIZE_DEFAULT,
    model: ChatModel | None = None,
) -> JudgeReport:
    """独立 judge（判定 D，**生产入口**）：按 ``batch_size`` 切批调用并返回 :class:`JudgeReport`。

    - **批量**：候选池按序切成 ``ceil(n / batch_size)`` 批，每批一次独立评审调用（同模型同组件）；
    - **闭集校验**：每批只接受**该批候选清单内**的代码，其余一律丢弃并记入
      :attr:`JudgeReport.out_of_set_codes`（防幻觉/代码漂移）；
    - **fail-open**：任一批异常 / 非 JSON 数组 / 池内候选漏判 ⇒ ``ok=False``（``degraded=True``），
      由调用方回退生成器分继续走 Gate，**绝不置** ``no_peers``；
    - 空池 / 空描述 ⇒ ``called=False``（未判定，不算降级）。
    """
    ordered, by_code = _ordered_pool(candidates)
    if not ordered or not str(business_description or "").strip():
        return JudgeReport(called=False)

    size = max(1, int(batch_size))
    chunks = [ordered[index : index + size] for index in range(0, len(ordered), size)]
    results: dict[str, dict[str, Any]] = {}
    out_of_set: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    failed = 0

    for index, chunk in enumerate(chunks):
        chunk_set = set(chunk)
        prompt = build_judge_prompt(business_description, [by_code[code] for code in chunk])
        payload, error = _invoke_json_array_ex(prompt, model)
        if payload is None:
            failed += 1
            errors.append(f"batch#{index}: {error}")
            missing.extend(chunk)
            continue
        _written, batch_out_of_set, _malformed = _collect_judge_rows(payload, chunk_set, results)
        out_of_set.extend(batch_out_of_set)
        # 漏判与「判定非法（verdict 不在三级内 / dims 缺失）」同属**未取得可用判定**
        missing.extend(code for code in chunk if not _judge_row_valid(results.get(code)))

    # 只保留可用行：非法判决不参与 Gate（调用方在 ok=False 时整体回退，本清理仅保证 results 自洽）
    results = {code: row for code, row in results.items() if _judge_row_valid(row)}
    return JudgeReport(
        results=results,
        batches=len(chunks),
        failed_batches=failed,
        missing_codes=tuple(missing),
        out_of_set_codes=tuple(out_of_set),
        errors=tuple(errors),
        called=True,
    )


# ── 判定 C：结构化维度 / 合成 overlap / 剔除明细格式（**单一实现**，供在线与 Gate 共用） ──
#: 权重唯一处（设计 §3.3/§3.5）：overlap = Σ w_i · dim_i。
DEFAULT_WEIGHTS: dict[str, float] = {
    "product": 0.40,
    "customer": 0.30,
    "business_model": 0.20,
    "material_tech": 0.10,
}

#: 阈值/下限：**由标注集 train 段标定**（设计 §6 Step 1 允许「仅用 train 重标定后复测」）；
#: 标定点 = train 段 C_dims 的 F1 最优（min_overlap 0.40 / product_floor 0.20）。
DEFAULT_PRODUCT_FLOOR = 0.20
DEFAULT_CUSTOMER_FLOOR = 0.20
DEFAULT_MIN_OVERLAP = 0.40

#: 消费侧最小对标数（设计 §8 决策 4）：1 只候选时中位数 = 该股本身，作基准无意义。
MIN_PEERS_DEFAULT = 2

DROP_JUDGE_REJECT = "judge reject"
DROP_PRODUCT_FLOOR = "产品重叠不足"
DROP_CUSTOMER_FLOOR = "客户重叠不足"

#: notes 里 reason 的字符上限（设计 §3.4 明细可审计）。
REASON_MAX_CHARS = 80


def normalize_dims(raw: Any) -> dict[str, float]:
    """``dims`` 归一到**四维齐全**的 ``[0,1]`` 表。

    输入可为 dict，也接受 **JSON 文本**（候选跨层传递时以 JSON 文本承载维度）；
    无法解析/非映射 → 全 0；缺字段/非法值 → 0.0。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {dim: 0.0 for dim in DIMS}
    if not isinstance(raw, dict):
        return {dim: 0.0 for dim in DIMS}
    out: dict[str, float] = {}
    for dim in DIMS:
        value = raw.get(dim)
        if value is None:
            out[dim] = 0.0
            continue
        try:
            out[dim] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            out[dim] = 0.0
    return out


def _has_dims(raw: Any) -> bool:
    """候选是否**携带**结构化维度（缺失时不做维度下限判定，只按 overlap 阈值，保持旧数据可用）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return False
    return isinstance(raw, dict) and any(dim in raw for dim in DIMS)


def overlap_score(dims: dict[str, float] | None, weights: dict[str, float] | None = None) -> float:
    """四维 → 合成 ``overlap``（权重唯一处；``weights`` 缺省取 :data:`DEFAULT_WEIGHTS`）。"""
    active = weights or DEFAULT_WEIGHTS
    table = dims or {}
    return sum(weight * table.get(dim, 0.0) for dim, weight in active.items())


def format_drop_note(item: dict[str, Any]) -> str:
    """单条剔除明细 → ``质量闸剔除 {code} {name}（{drop}）：{reason}``（reason ≤80 字符）。"""
    code = str(item.get("code") or "?")
    name = str(item.get("name") or "")
    drop = str(item.get("drop") or "")
    reason = str(item.get("reason") or "").strip()
    if len(reason) > REASON_MAX_CHARS:
        reason = reason[:REASON_MAX_CHARS] + "…"
    return f"质量闸剔除 {code} {name}（{drop}）：{reason}"
