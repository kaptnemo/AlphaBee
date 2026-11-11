"""``params/registry.yaml`` 的加载、校验与参数指纹。

三层职责：

1. :func:`load_registry`：读 YAML 并构造 :class:`ParamEntry` 列表；注册表缺失、YAML
   损坏、结构非法一律抛 :class:`ParamRegistryError`（**明确错误**）。本模块不吞异常：
   fail-open 由 :mod:`alphabee.params.resolvers` 负责（生产调用方永不看到异常）；
2. :func:`validate_entries`：对已构造的条目做领域校验（id/kind/status/unit/domain/
   类型/bounds/组约束/wired_in 字段存在性）。可被测试与未来实验文件复用；
3. :func:`compute_params_hash`：按设计 §4.7 对**最终生效值**算 16 位 sha256 前缀。

"未知 id" 的边界：注册表允许出现尚未接线的条目（``wired_in`` 留空 + ``notes`` 说明，
设计 §4.3），因此本模块**不**拒绝"未知 id"；未知 id 的 fail-open 语义发生在解析层——
config 覆盖里的未知 id 由 resolvers 忽略并 warn，registry 内属于当前参数组前缀但无法
绑定 typed 字段的条目同样忽略并 warn（见 ``resolvers``）。

本模块只依赖 stdlib + PyYAML + ``alphabee.PROJECT_ROOT``；不 import config / 业务模块，
因此导入期不读配置文件、不触发 settings 构造。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from alphabee import PROJECT_ROOT
from alphabee.params.models import EvidenceParams, ReportQualityParams, ScoreParams

#: ``load_registry()`` 的默认注册表路径（``PROJECT_ROOT/params/registry.yaml``）。
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "params" / "registry.yaml"

#: 当前支持的注册表 schema / registry 版本（设计 §4.3）。
SCHEMA_VERSION = 1
REGISTRY_VERSION = 1

#: 允许的 ``kind``：``structure`` 结构常量不允许进 registry（设计 §4.2/§4.3）。
ALLOWED_KINDS: tuple[str, ...] = ("policy", "technical")
#: 参数状态机（设计 §4.3）。
ALLOWED_STATUSES: tuple[str, ...] = ("active", "shadow", "placeholder", "frozen", "deprecated")
#: 单位枚举（设计 §4.3）。
ALLOWED_UNITS: tuple[str, ...] = ("score", "pp", "ratio", "count", "nats")
#: 领域枚举（设计 §4.3）。
ALLOWED_DOMAINS: tuple[str, ...] = ("midterm", "report", "data")
#: 取值证据来源（§4.3：``design`` = 设计初值、未校准）。
ALLOWED_SOURCES: tuple[str, ...] = ("backtest", "expert", "literature", "design", "placeholder")

#: ``wired_in`` 可绑定的 typed params 类（class 名 → dataclass）。
PARAMS_CLASSES: dict[str, type] = {
    "ScoreParams": ScoreParams,
    "EvidenceParams": EvidenceParams,
    "ReportQualityParams": ReportQualityParams,
}

#: 参数组前缀：用于把"未接线/无法绑定"的注册表条目录到正确的解析器上（仅用于诊断 warn）。
CLASS_ID_PREFIXES: dict[str, tuple[str, ...]] = {
    "ScoreParams": ("midterm.score.", "midterm.data."),
    "EvidenceParams": ("midterm.evidence.",),
    "ReportQualityParams": ("report.",),
}

#: 注册表必填字段（设计 §4.3）。
REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "id",
    "value",
    "unit",
    "domain",
    "kind",
    "status",
    "source",
    "version",
    "owner",
    "review_condition",
    "overridable",
)
#: 注册表可选字段（设计 §4.3；``notes`` 建议填写）。
OPTIONAL_ENTRY_FIELDS: tuple[str, ...] = ("wired_in", "bounds", "notes", "group", "constraint")
#: 注册表顶层字段。
REGISTRY_TOP_LEVEL_FIELDS: tuple[str, ...] = ("schema_version", "registry_version", "entries")

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$")
_WIRED_IN_PATTERN = re.compile(r"^(?P<cls>[A-Za-z_][A-Za-z0-9_]*)\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)")
_GROUP_SUM_EPS = 1e-9


class ParamRegistryError(ValueError):
    """注册表缺失/结构非法/绑定非法。resolvers 捕获后 fail-open 回内置默认。"""


@dataclass(frozen=True)
class ParamEntry:
    """``params/registry.yaml`` 的一条参数（字段语义见设计 §4.3）。

    ``bounds`` 为 ``None`` 表示未声明边界；``constraint`` 形如 ``{"sum": 1.0}``，
    仅在与 ``group`` 同时出现时生效。
    """

    id: str
    value: float | int
    unit: str
    domain: str
    kind: str
    status: str
    source: str
    version: int
    owner: str
    review_condition: str
    overridable: bool
    wired_in: str = ""
    bounds: tuple[float, float] | None = None
    notes: str = ""
    group: str = ""
    constraint: dict[str, float] = field(default_factory=dict)


def parse_wired_in(wired_in: str) -> tuple[str, str] | None:
    """解析 ``wired_in`` 的首个绑定，返回 ``(class_name, field_name)``。

    允许 ``"ScoreParams.min_consensus_coverage + build_numeric_evidence(...)"`` 这种
    "主绑定 + 说明性文字"形式（设计附录 B 第 11 条）：只解析第一个 ``+`` 段，
    其余文字不参与校验。空字符串 / 纯空白返回 ``None``（未接线）。
    """
    head = (wired_in or "").split("+", 1)[0].strip()
    if not head:
        return None
    match = _WIRED_IN_PATTERN.match(head)
    if match is None:
        return None
    return match.group("cls"), match.group("field")


def _field_of(params_cls: type, field_name: str) -> Any:
    for item in fields(params_cls):
        if item.name == field_name:
            return item
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ParamRegistryError(message)


def validate_entries(entries: list[ParamEntry] | tuple[ParamEntry, ...]) -> None:
    """校验注册表条目；非法时抛 :class:`ParamRegistryError`（消息带 id / 字段名）。

    覆盖设计 §4.3 的必填字段、枚举、类型、bounds 与组约束，以及 "``wired_in`` 绑定的
    typed params 字段必须存在"（设计 §4.6.2 的三组字段）。
    """
    _require(bool(entries), "参数注册表为空（entries 至少 1 条）")

    seen_ids: set[str] = set()
    for entry in entries:
        _validate_single_entry(entry)
        _require(entry.id not in seen_ids, f"参数 id 重复：{entry.id}")
        seen_ids.add(entry.id)

    _validate_groups(list(entries))


def _validate_single_entry(entry: ParamEntry) -> None:
    label = f"参数 {entry.id!r}"

    _require(bool(entry.id) and isinstance(entry.id, str), f"{label}：id 必须是非空字符串")
    _require(_ID_PATTERN.match(entry.id) is not None, f"{label}：id 必须为小写点分 domain.area.name")

    _require(_is_number(entry.value), f"{label}：value 必须是数值（int/float），当前为 {entry.value!r}")

    _require(
        entry.kind in ALLOWED_KINDS,
        f"{label}：kind 非法（{entry.kind!r}）；结构常量不得写入 registry，允许值 {ALLOWED_KINDS}",
    )
    _require(entry.status in ALLOWED_STATUSES, f"{label}：status 非法（{entry.status!r}），允许值 {ALLOWED_STATUSES}")
    _require(entry.unit in ALLOWED_UNITS, f"{label}：unit 非法（{entry.unit!r}），允许值 {ALLOWED_UNITS}")
    _require(entry.domain in ALLOWED_DOMAINS, f"{label}：domain 非法（{entry.domain!r}），允许值 {ALLOWED_DOMAINS}")
    _require(entry.source in ALLOWED_SOURCES, f"{label}：source 非法（{entry.source!r}），允许值 {ALLOWED_SOURCES}")

    _require(
        isinstance(entry.version, int) and not isinstance(entry.version, bool) and entry.version >= 1,
        f"{label}：version 必须 >= 1 的整数，当前为 {entry.version!r}",
    )
    _require(isinstance(entry.owner, str) and bool(entry.owner.strip()), f"{label}：owner 必须是非空字符串")
    _require(
        isinstance(entry.review_condition, str) and bool(entry.review_condition.strip()),
        f"{label}：review_condition 必须是非空字符串（何时必须复核）",
    )
    _require(isinstance(entry.overridable, bool), f"{label}：overridable 必须是布尔值")
    _require(isinstance(entry.notes, str), f"{label}：notes 必须是字符串")
    _require(isinstance(entry.wired_in, str), f"{label}：wired_in 必须是字符串")

    if entry.bounds is not None:
        low, high = entry.bounds
        _require(_is_number(low) and _is_number(high), f"{label}：bounds 必须是两个数值，当前为 {entry.bounds!r}")
        _require(low <= high, f"{label}：bounds 下界 {low} 大于上界 {high}")
        _require(
            low <= float(entry.value) <= high,
            f"{label}：value {entry.value} 越界 bounds [{low}, {high}]",
        )

    if entry.wired_in:
        binding = parse_wired_in(entry.wired_in)
        _require(
            binding is not None,
            f"{label}：wired_in 无法解析（期望 'ScoreParams.field' 形式），当前为 {entry.wired_in!r}",
        )
        assert binding is not None  # for type checkers; _require 已保证
        class_name, field_name = binding
        _require(class_name in PARAMS_CLASSES, f"{label}：wired_in 指向未知 params 类 {class_name!r}")
        _require(
            _field_of(PARAMS_CLASSES[class_name], field_name) is not None,
            f"{label}：wired_in 字段 {class_name}.{field_name} 不存在",
        )
        _require(
            entry.id.startswith(CLASS_ID_PREFIXES.get(class_name, ())),
            f"{label}：id 前缀与 wired_in 绑定类 {class_name} 不一致（未知 id）",
        )


def _validate_groups(entries: list[ParamEntry]) -> None:
    groups: dict[str, list[ParamEntry]] = {}
    for entry in entries:
        if entry.constraint and not entry.group:
            raise ParamRegistryError(f"参数 {entry.id!r}：constraint 必须与 group 同时出现")
        if entry.group:
            groups.setdefault(entry.group, []).append(entry)

    for group_name, members in groups.items():
        _require(
            len(members) >= 2,
            f"参数组 {group_name!r} 只有 {len(members)} 条成员（组约束至少需要 2 条才能自检）",
        )
        constraints = [member.constraint for member in members]
        _require(
            all(constraint == constraints[0] for constraint in constraints),
            f"参数组 {group_name!r} 的 constraint 声明不一致：{constraints}",
        )
        constraint = constraints[0]
        _require(
            set(constraint) <= {"sum"},
            f"参数组 {group_name!r}：不支持的 constraint 键 {sorted(set(constraint) - {'sum'})}（仅支持 sum）",
        )
        if "sum" not in constraint:
            continue
        target = constraint["sum"]
        _require(_is_number(target), f"参数组 {group_name!r}：sum 约束必须是数值")
        member_ids = [member.id for member in members]
        _require(len(set(member_ids)) == len(member_ids), f"参数组 {group_name!r}：成员 id 重复")
        total = sum(float(member.value) for member in members)
        _require(
            abs(total - float(target)) <= _GROUP_SUM_EPS,
            f"参数组 {group_name!r}：注册表取值之和 {total:.12g} 不满足 sum={target}",
        )


def load_registry(path: str | Path | None = None) -> list[ParamEntry]:
    """加载并校验注册表；异常统一转成 :class:`ParamRegistryError`。

    默认路径 :data:`DEFAULT_REGISTRY_PATH`；调用方（resolvers）负责 fail-open。
    """
    resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ParamRegistryError(f"无法读取参数注册表 {resolved}：{exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParamRegistryError(f"参数注册表 YAML 解析失败 {resolved}：{exc}") from exc

    entries = _parse_registry(raw, resolved)
    validate_entries(entries)
    return entries


def _parse_registry(raw: Any, source: Path) -> list[ParamEntry]:
    _require(isinstance(raw, dict), f"参数注册表 {source} 顶层必须是映射，当前为 {type(raw).__name__}")
    assert isinstance(raw, dict)
    unknown_top = sorted(set(raw) - set(REGISTRY_TOP_LEVEL_FIELDS))
    _require(not unknown_top, f"参数注册表 {source} 含未知顶层字段 {unknown_top}")
    _require(raw.get("schema_version") == SCHEMA_VERSION, f"参数注册表 {source} schema_version 必须为 {SCHEMA_VERSION}")
    _require(
        raw.get("registry_version") == REGISTRY_VERSION,
        f"参数注册表 {source} registry_version 必须为 {REGISTRY_VERSION}",
    )
    raw_entries = raw.get("entries")
    _require(isinstance(raw_entries, list) and bool(raw_entries), f"参数注册表 {source} entries 必须是非空列表")
    assert isinstance(raw_entries, list)  # mypy 收窄（_require 只负责运行时校验）

    entries: list[ParamEntry] = []
    for index, raw_entry in enumerate(raw_entries):
        _require(
            isinstance(raw_entry, dict),
            f"参数注册表 {source} entries[{index}] 必须是映射，当前为 {type(raw_entry).__name__}",
        )
        assert isinstance(raw_entry, dict)
        entries.append(_parse_entry(raw_entry, source, index))
    return entries


def _parse_entry(raw: dict[str, Any], source: Path, index: int) -> ParamEntry:
    entry_id = raw.get("id", f"entries[{index}]")
    label = f"参数注册表 {source} 条目 {entry_id!r}"
    unknown_keys = sorted(set(raw) - set(REQUIRED_ENTRY_FIELDS) - set(OPTIONAL_ENTRY_FIELDS))
    _require(not unknown_keys, f"{label}：含未知字段 {unknown_keys}")
    missing = [name for name in REQUIRED_ENTRY_FIELDS if name not in raw]
    _require(not missing, f"{label}：缺必填字段 {missing}")

    bounds_raw = raw.get("bounds")
    bounds: tuple[float, float] | None
    if bounds_raw is None:
        bounds = None
    else:
        _require(isinstance(bounds_raw, (list, tuple)) and len(bounds_raw) == 2, f"{label}：bounds 必须是 [min, max]")
        assert isinstance(bounds_raw, (list, tuple))
        bounds = (bounds_raw[0], bounds_raw[1])

    constraint_raw = raw.get("constraint") or {}
    _require(isinstance(constraint_raw, dict), f"{label}：constraint 必须是映射，如 {{sum: 1.0}}")
    assert isinstance(constraint_raw, dict)
    for key, value in constraint_raw.items():
        _require(isinstance(key, str), f"{label}：constraint 键必须是字符串")
        _require(_is_number(value), f"{label}：constraint[{key!r}] 必须是数值，当前为 {value!r}")

    def _optional_str(name: str) -> str:
        value = raw.get(name, "")
        if value is None:
            return ""
        _require(isinstance(value, str), f"{label}：{name} 必须是字符串，当前为 {value!r}")
        return str(value)

    return ParamEntry(
        id=str(raw["id"]),
        value=raw["value"],
        unit=str(raw["unit"]),
        domain=str(raw["domain"]),
        kind=str(raw["kind"]),
        status=str(raw["status"]),
        source=str(raw["source"]),
        version=raw["version"],
        owner=str(raw["owner"]),
        review_condition=str(raw["review_condition"]),
        overridable=raw["overridable"],
        wired_in=_optional_str("wired_in"),
        bounds=bounds,
        notes=_optional_str("notes"),
        group=_optional_str("group"),
        constraint=dict(constraint_raw),
    )


def compute_params_hash(score: ScoreParams, evidence: EvidenceParams) -> str:
    """按设计 §4.7 计算 16 位参数指纹。

    只对**最终生效值**做哈希：``midterm.score.*`` + ``midterm.evidence.*`` 键 → 排序 →
    ``f"{float(v):.12g}"`` → sha256 前 16 位。``profile`` / ``overrides`` / registry 的
    status/notes 均不参与（改注释/状态不改变指纹）。
    """
    values: dict[str, Any] = {
        **{f"midterm.score.{key}": value for key, value in asdict(score).items()},
        **{f"midterm.evidence.{key}": value for key, value in asdict(evidence).items()},
    }
    payload = "\n".join(f"{key}={float(value):.12g}" for key, value in sorted(values.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
