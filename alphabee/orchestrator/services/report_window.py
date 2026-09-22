"""财报原文窗口（P1 / W3）：从**本地已解析**的财报中选出叙事章节喂给证据抽取。

设计依据：``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.1（P1）。本模块是
``nodes/midterm.py::_window_texts()`` 的**唯一**原文来源，解决「定性证据抽取
（Stage A/B）在主链从未真正生效」这一缺口（窗口过去只含已验证冲突 explanation）。

工程约束（§15.0 C，六期共同遵守）
--------------------------------
1. **纯规则、零 LLM、无网络**：只读本地 ``reports/``，不做"下载 → OCR → 解析"
   （那会把分钟级延迟引入分析主链，§15.1-B-1）；
2. **fail-open**：任何一步失败 → 返回带 ``reason`` 的空窗口 + ``logger.warning``，
   **绝不抛异常**，绝不打断 run（§15.0 C-3）；
3. **不新增判定与阈值**：本模块不做多空方向判定，只做"章节选择 + 字符预算"；
4. **import 期不读配置**（§15.0 / §14.6：``config/__init__.py`` 有模块级
   ``settings = get_settings()``），开关读取一律在函数体内惰性进行；
5. **不依赖 orchestrator 节点/图/数据层**：只依赖 ``financial_report.report_parser``
   的路径函数（该模块是本仓库既有的解析层，无重依赖、无 ``tushare.set_token`` 副作用）。

数据入口的两个设计决策（§15.1-B，均有代码依据）
---------------------------------------------
* 以 manifest 的 ``full_text_path``（``reports_full`` 全文副本）为主入口，而非章节目录树：
  实测 ``reports/`` 下同时存在"嵌套章节树"与"平铺 ``<报告名>.md``"两种历史布局，
  "读全文 + 按标题切分"对两种布局都稳健；
* 按 **6 位代码** glob 目录名（``*(<代码>)``），不按公司名拼路径：公司名需联网解析
  （``links._resolve_company``），而目录名已含代码，可离线确定性匹配。

序口径（**本模块相对设计文档 §15.1-B 的字面措辞有一处显式偏差，必须披露**）
----------------------------------------------------------------------------
设计文档 §15.1-B-2 的字面措辞是"按 ``created_at`` 倒序取**最新一期**"。**本模块不这样做**：

* **主序 = 报告期**（:func:`_parse_report_period` 解析 ``report_period`` 的 ``(年, 期内序)``，
  期内序 = 第一季度 < 半年度/第二季度 < 第三季度 < 年度报告）；**``created_at`` 仅作 tie-break**
  （同一报告期的多份产物取最后入库者，如"（修订后）"版本）；
* **``as_of`` 过滤同样以报告期为准**（报告期期末日晚于 ``as_of`` 者不得入选），
  **不**用 ``created_at`` 判"是否晚于 ``as_of``"（报告期不可解析时才退回 ``created_at`` 兜底）。
* **偏差理由（实测、可复现）**：``created_at`` 是**解析入库时刻**，同一批 ingest 按目录序遍历
  ⇒ 与报告期**无因果关系**。命令枚举 ``reports/**/.report_manifest.json`` = 22 家公司 /
  142 份 manifest（``find reports -name .report_manifest.json | wc -l``），按 ``created_at`` 倒序选出的"最新一期"与按报告期语义最新者在 **21/22 家**
  上不一致；极端样本 ``reports/工业富联(601138)``：``created_at`` 最新一份是
  **2023 年年度报告**（``2026-08-22T07:05``），而报告期最新一份是 **2026 年半年度报告**
  （``2026-08-21T23:55``，恰是 12 份里 ``created_at`` **最旧**的一份）。
  按字面读法，窗口会接进一份**两年前的陈旧年报**，§11 验收 3 的语义
  「**期间出现的**新财报被接进窗口」在真实数据上**落空**（而按 §15.1-G 的字面断言仍会假绿）。
  该偏差已登记在 ``docs/roadmap/ROADMAP.md``「行为变更登记」的 P1 行内。

章节匹配与文本边界（两条显式约定，影响 ``chars`` 上界与预算消耗）
--------------------------------------------------------------
* **子串匹配**：:data:`SECTION_GROUPS` 的关键词用 ``in`` 做**子串**判断（非全等、非正则）。
  ⇒ 真实年报第 40 行那样的 ``## 十、 重大风险提示`` 会命中 ``risk`` 组；这是**预期**
  （它确实是叙事型风险章节，不是"财务报表附注/审计报告"那类噪声）。
* **文本边界 = 子树（F8 口径已变更）**：命中章节的文本**延伸到下一个同级或更高级标题
  为止**（:func:`_subtree_span`），即**父章节的正文包含其全部子标题段落**。
* **组归属与去重 = 顶层声明制（F8 裁定 A）**：自上而下遍历标题树，某节点命中白名单即
  **认领其整棵子树**，其文本**归属该父章节所在的组**，且**不再向内层继续认领** ⇒
  *子树内其他命中关键词（例如 ``mda`` 父章节里的"风险"子标题）不产生第二次认领*，
  **一条文本只入窗一次**（不会重复计 ``chars``）。
  ⇒ ``WindowSection.chars == len(text)`` 仍恒成立，窗口总字符数仍是 ``max_chars`` 的
  **硬上限**；``max_sections`` 计**被认领的章节数**（一棵巨大子树仍只算 1 段）。

  **为什么改成子树（取舍与实测）**：旧口径"到下一个标题为止"（叶节点边界）下，父章节常因
  自身正文为空而被丢弃、白名单又只匹配标题，导致**真实叙事文本几乎进不了窗口**——命中的
  多是 ``## （二） 非主营业务导致利润重大变化的说明`` ＋ ``□适用 √不适用`` 这类**占位行**。
  22 家本地全库实测（命令 ``find reports -name .report_manifest.json | wc -l`` = 142 份 /
  22 家，``as_of=2026-12-31``，逐家 ``select_report_window`` 累加 ``chars``）：
  V1/V2/V3 **分层登记**（定义与逐家数字见 ``tmp/rp1_verify/probe_i1_boundary2.py`` 探针实测）：

  * **V1 叶边界（旧口径）**：合计 **88,164 字符**（全库全文 0.48%），**5/22 家**薄于 1000 字符，
    ``601138`` 仅 **253 字符**（其 939 KB 全文的 0.026%）。
  * **V2 子树、未加分配规则**：合计 **251,016 字符**（本模块口径；探针独立复算 **251,073**，57 字符差为
    "原始标题行 vs 归一化重建"的写法差异）；**窗口内噪声占比 0.0%**（V2 == V3）；薄窗口（<1000 字符）
    **1/22**（唯 ``300274`` 无白名单叙事章节）；``601138`` 此时**一票吃满** ``max_chars=12000``
    （``truncated=True``）⇒ 正是裁定 C 要修的病。
  * **V3 = 现值（子树 + 按命中组等额分配 + 余额顺延，本模块交付口径）**：合计 **174,732 字符**
    （全库全文 **0.955%**）；**``601138`` = 4,101 字符 / 3 段**（``mda`` 4,000 + ``business`` 46 +
    ``risk`` 55，``truncated=True``）——**该值为对拍基准**（与 ROADMAP P1 行逐字一致）；薄窗口
    （<1000 字符）**1/22**。
  * 注：探针的"薄窗口"判据是"``≤3 段`` **或** ``<1000 字符``"，按该判据现值 **19/22** 家被标薄 ——
    其中 18 家由**段数**判据所致（段数 ≤3 是等额分配的预期形态），按**字符**判据仍只有 ``300274``（0 字符）。
  **为什么不是"切得更浅/再剔噪声子树"**：实测"子树边界"与"子树边界再剔除噪声子树"
  **逐字符相同**（后者减 0 字符）⇒ 窗口里的噪声并非来自"切得深"，而是来自**标题级白名单
  本身的取舍**；故不需要引入新的排除规则（也就不会新增判定/阈值）。

  **代价（显式披露）**：子树边界会把叙事章节**内部的数值表格**一并带入（例如「第三节
  管理层讨论与分析」下的「(1) 主营业务分行业、分产品…情况」表）；该风险由**标题级白名单
  与预算双重约束**兜住：只有命中白名单的子树才入窗，且总字符数硬顶在 ``max_chars``。

  **预算分配（F8 裁定 C）**：``max_chars`` 仍是总硬上限，但**按"有内容的命中组"等额切分**（每组 ``max_chars // 命中组数``，整除余数同样顺延），组内按文档序取章节、
  每段截到该组额度，**未用完的余额顺延给下一个命中组**。**修正了什么（实测）**：早前"按组序
  顺序吃满总预算"在子树边界下**坍缩**——``mda`` 的首棵子树（实测 47,810 字符）一票吃满
  12,000 ⇒ ``risk`` 组 **0/22 家**能拿到内容、**21/22 家只剩 ≤1 个组**、``max_sections=12``
  形同虚设；等额分配后 **``risk`` 恢复到 10/22 家**、**只剩 ≤1 个组降到 11/22 家**。
  **取舍（显式披露）**：① 大子树会被截到**其组份额**（单段不再是完整子树），全库合计字符数
  由 251,016 降为 **174,732**（用总量换组覆盖）；② **残余**：11/22 家的风险标题（如
  ``十、 公司面临的风险和应对措施``）**位于被认领的 ``mda`` 子树之内**，按顶层声明制归属
  ``mda``、不再单独认领（另 1/22 家 ``300274`` 报告内无 risk 白名单标题）⇒ 这些家的风险
  文本受 ``mda`` 份额截断影响；要改变归属须动顶层声明制（属另一次口径变更）。
  **组别构成（22 家实测）**：``mda`` 164,000 字符（93.9%）/ ``business`` 8,590（4.9%）/
  ``risk`` 2,142（1.2%）。该分配规则与两条不变量由用例长期钉住：
  ``test_total_chars_never_exceeds_max_chars_invariant`` / ``test_total_chars_invariant_holds_for_whole_local_corpus``
  （总字符 ≤ ``max_chars`` 恒成立）与 ``test_real_sample_risk_group_nonempty_under_equal_split``
  （``risk`` 贡献 > 0）。判别力实测：忠实复现旧 FCFS 时 risk 钉子变红、total_chars 钉子仍绿。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alphabee.config import ReportWindowSettings
from alphabee.financial_report.report_parser import reports_root

logger = logging.getLogger(__name__)

__all__ = [
    "SECTION_GROUPS",
    "ReportWindow",
    "WindowSection",
    "report_window_enabled",
    "select_report_window",
]


class WindowSection(BaseModel):
    """命中的单个章节：标题 + 正文 + 字符数 + 命中的白名单组 key。"""

    title: str = ""
    text: str = ""
    chars: int = 0
    group: str = ""  # 命中的章节组 key（便于追溯"这条证据来自哪类章节"）


class ReportWindow(BaseModel):
    """一次窗口选择的结果。``reason`` 非空表示未取到（**调用方负责显式记账**）。"""

    symbol: str = ""
    report_name: str = ""
    report_period: str = ""
    page_count: int | None = None
    source_path: str = ""  # 取自 manifest 的 full_text_path（reports_full 全文副本）
    sections: list[WindowSection] = Field(default_factory=list)
    chars: int = 0
    truncated: bool = False
    reason: str = ""  # 未取到的原因；空串 = 成功（有 sections）


#: 章节白名单：组内无序、**组间有序**（先命中前面的组）。
#: 只取"叙事章节"；显式排除 财务报表附注 / 审计报告 / 公司治理 / 董监高 / 股本变动
#: （超长且对 thesis 方向判定是噪声）。
SECTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mda", ("管理层讨论与分析", "经营情况讨论与分析", "管理层讨论", "经营分析")),
    ("business", ("主营业务", "经营模式", "所处行业", "行业情况", "核心竞争力")),
    ("risk", ("风险因素", "面临的风险", "主要风险", "风险提示")),
)

#: Markdown 标题行（``#`` ~ ``######``）；全文副本由 OCR loader 产出，顶层标题为 ``#``。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

#: 交易所后缀归一化（``300750.SZ`` / ``sh600000`` → 6 位代码）。
_CODE_RE = re.compile(r"(\d{6})")

#: manifest 的文件名（与 ``report_parser.write_markdown_report_folder`` 一致）。
_MANIFEST_NAME = ".report_manifest.json"


def _settings() -> ReportWindowSettings | None:
    """运行时读取 ``settings.report_window``（§15.1-F），容忍配置缺失/异常。

    **不在 import 期读配置**（§15.0 / §14.6：``config/__init__.py`` 有模块级
    ``settings = get_settings()``）。缺段、非预期类型或读取异常 → ``None``，
    由调用方回落 §15.1-F 的文档默认值。
    """
    try:
        from alphabee.config import get_settings

        section = getattr(get_settings(), "report_window", None)
        return section if isinstance(section, ReportWindowSettings) else None
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断 run
        logger.warning("report_window settings unavailable (fail-open): %s", exc)
        return None


def report_window_enabled() -> bool:
    """读取 ``report_window.enabled`` 开关（§15.1-F），容忍配置缺失。

    **该开关同时门控「窗口内容」与「D1 记账」**：调用方
    （``nodes/midterm.py::_window_texts``）把本函数的返回值作为 ``enabled`` 透传给
    :func:`select_report_window`，故 ``False`` 时选中逻辑在读配置/读盘**之前**即返回
    ``reason="disabled"`` 的空窗口 ⇒ 窗口内容与旧实现
    ``_conflict_explanations(artifacts) or None`` **逐字相同且不读盘**。

    **该自述的可复算口径（不 stub ``select_report_window``）**：

    * 开关透传 + 内容逐字等价::

          poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
              pytest tests/orchestrator/test_report_window.py -k switch_off -q
          # 期望：1 passed（反 stub 哨兵：select_report_window 真被走到即 reports_root 抛断言）

    * 开关关闭时**不读配置**（短路在读配置之前）::

          poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
              pytest tests/orchestrator/test_report_window.py -k disabled_selection_reads_nothing -q
          # 期望：1 passed

    * 配置不可读 ⇒ 回落 ``False``（fail-open 方向 = 退回现状）::

          poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
              pytest tests/orchestrator/test_report_window.py -k report_window_enabled -q
          # 期望：2 passed

    任何一处期望值不成立，即说明"开关只门控 D1 记账、不门控窗口内容"（M5 型变异）复现。

    **fail-open 的方向是"退回现状"**：与 ``deviation.detection.enabled``（异常 → ``True``）
    相反，本开关在**配置不可读/非预期**时返回 ``False`` —— 因为 ``True`` 会新增
    ``report_window_unavailable`` issue 并改变窗口内容，属**新行为**；配置不可用时不应
    凭空打开新行为（§15.0 C-4）。缺 ``report_window`` 段（旧 config.yaml）→ 用
    :class:`ReportWindowSettings` 的默认值（``True``，与 §15.1-F 一致）。
    """
    section = _settings()
    if section is None:
        return _FAIL_OPEN_ENABLED
    return bool(getattr(section, "enabled", _DEFAULT_ENABLED))


#: §15.1-F 的配置默认值（``report_window.enabled: true``）。
_DEFAULT_ENABLED = True

#: §15.1-F 的窗口预算默认值（与 ``ReportWindowSettings`` 的默认值逐项一致）。
_DEFAULT_MAX_CHARS = 12_000
_DEFAULT_MAX_SECTIONS = 12

#: 配置读取异常时的回落值：**退回现状**（不新增 issue、不改窗口内容）。
_FAIL_OPEN_ENABLED = False


def _symbol_code(symbol: str) -> str:
    """取 6 位代码（容错 ``300750.SZ`` / ``sz300750`` / 带空白的输入）。"""
    match = _CODE_RE.search(symbol or "")
    return match.group(1) if match else ""


def _read_json(path: Path) -> Any:
    """读 JSON（失败返回 ``None``，不抛）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - manifest 不可解析由调用方转成 reason
        return None


def _subdirs(path: Path) -> list[Path]:
    """列出子目录（按名排序；不可读 → 空列表，不抛）。"""
    try:
        return sorted(p for p in path.glob("*") if p.is_dir())
    except Exception as exc:  # noqa: BLE001 - 目录不可读 → 视作无内容
        logger.warning("report_window: 枚举目录失败（按空处理）: %s", exc)
        return []


def _manifest_candidates(root: Path, code: str) -> tuple[list[dict[str, Any]], bool]:
    """枚举该代码的本地报告 manifest。

    Returns:
        ``(candidates, matched_dir)``：candidates 为已解析的 manifest dict（按 ``created_at``
        倒序）；``matched_dir`` 表示"存在 ``*(<代码>)`` 目录"（用于区分 ``no_local_report``
        与 ``manifest_unreadable``）。目录存在但一个 manifest 都读不出 → 返回空列表 +
        ``True``。
    """
    candidates: list[dict[str, Any]] = []
    try:
        company_dirs = sorted(p for p in root.glob(f"*{code}*") if p.is_dir())
    except Exception as exc:  # noqa: BLE001 - 目录不可读按"无本地报告"处理
        logger.warning("report_window: glob 本地报告目录失败: %s", exc)
        return [], False
    if not company_dirs:
        return [], False

    # 只看第一个匹配目录（同一代码理论上只应有一个"<公司名>(<代码>)"目录）。
    # 注意：manifest 文件名是**隐藏文件**（``.report_manifest.json``），``Path.glob`` 的
    # ``*`` 不匹配以点开头的名字，故只能"先列目录、再显式拼 manifest 名"。
    # 真实布局为 ``<公司名>(<代码>)/财报/<报告期>/.report_manifest.json``（两跳）；
    # 兼容旧平铺布局 ``<报告名>/.report_manifest.json``（一跳）。
    company_dir = company_dirs[0]
    report_dirs = list(_subdirs(company_dir))
    for first_level in _subdirs(company_dir):
        report_dirs.extend(_subdirs(first_level))
    for report_dir in report_dirs:
        data = _read_json(report_dir / _MANIFEST_NAME)
        if isinstance(data, dict):
            data["_manifest_path"] = str(report_dir / _MANIFEST_NAME)
            candidates.append(data)
    candidates.sort(key=_report_order_key, reverse=True)
    return candidates, True


#: 报告期序（**年内**先后）：第一季度 < 半年度/第二季度 < 第三季度 < 年度报告。
#: 依据：A 股披露节奏（Q1 → 半年报 → Q3 → 年报），报告期越晚 = 信息越新。
_PERIOD_QUARTER = 1
_PERIOD_HALF = 2
_PERIOD_THIRD = 3
_PERIOD_ANNUAL = 4

#: ``report_period`` 关键字 → ``(期内序, 报告期末月日)``。**顺序敏感**：
#: ``半年度`` 必须先于 ``年度``（"2026年半年度报告" 同时含 "年度" 子串）。
_PERIOD_KEYWORDS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("半年度", "半年报"), _PERIOD_HALF, "06-30"),
    (("第一季度", "一季度", "1季度"), _PERIOD_QUARTER, "03-31"),
    (("第二季度", "二季度", "2季度"), _PERIOD_HALF, "06-30"),
    (("第三季度", "三季度", "3季度"), _PERIOD_THIRD, "09-30"),
    (("年度", "年报"), _PERIOD_ANNUAL, "12-31"),
)

#: 4 位年份（``report_period`` 是**报告名全称**，年份嵌在其中，如"…股份有限公司2026年半年度报告"）。
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _parse_report_period(period: str) -> tuple[int, int, str]:
    """解析 ``report_period`` → ``(年, 期内序, 期末月日)``；不可解析 → ``(0, 0, "")``。

    **这是本模块的「最新一期」判据**（见 :func:`select_report_window` 的序口径说明）：
    A 股财报的新旧由**报告期**决定，而不是由"文件何时进入本地库"（``created_at``）决定。
    """
    text = period or ""
    year_match = _YEAR_RE.search(text)
    if year_match is None:
        return (0, 0, "")
    year = int(year_match.group(0))
    for keywords, order, month_day in _PERIOD_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return (year, order, month_day)
    return (year, 0, "")


def _period_end(period: str) -> str:
    """报告期**期末日**（``YYYY-MM-DD``）—— ``as_of`` 过滤用；不可解析 → 空串。"""
    year, order, month_day = _parse_report_period(period)
    if not year or not order or not month_day:
        return ""
    return f"{year}-{month_day}"


def _report_order_key(item: dict[str, Any]) -> tuple[int, int, str]:
    """排序键（**主序 = 报告期**，``created_at`` 仅作 tie-break）：

    1. ``(年, 期内序)`` —— 越大越新（主序）；
    2. ``created_at`` —— 同一报告期的多份产物（如同一期的"（修订后）"版本）取**最后入库**者。

    报告期不可解析（无 4 位年份/无期间关键字）→ ``(0, 0, …)``，即排到最后（不猜测），
    且其 ``as_of`` 判定退回 :func:`_manifests_before` 的 ``created_at`` 兜底。
    """
    year, order, _ = _parse_report_period(str(item.get("report_period") or ""))
    return (year, order, str(item.get("created_at") or ""))


def _manifests_before(candidates: list[dict[str, Any]], as_of: str | None) -> list[dict[str, Any]]:
    """``as_of`` 过滤：只保留**报告期期末日与入库时刻都不晚于 ``as_of``** 的报告。

    两个条件都必须满足（F5：消除 point-in-time 前视）：

    * **报告期**期末日 ≤ ``as_of``（主序口径，见模块 docstring）——报告期可解析时用它；
    * **入库时刻** ``created_at`` 日期前缀 ≤ ``as_of``——``as_of`` 时点**尚未入库**的产物
      不得进入窗口（历史回放 / 带历史 ``as_of`` 的 run 下的前视消除）。该条件与下面
      "报告期不可解析"分支复用同一字段与同一写法，**不新增任何阈值或判定**；
    * 报告期不可解析 → 只看 ``created_at`` 日期前缀（不猜报告期）；
    * ``as_of`` 为空 → 不做时间过滤。

    实时 run（``as_of`` = 今天）下 ``created_at`` 恒 ≤ 今天，故该条件不改变结果——在冻结树上
    以限定作用域命令 ``find reports -name .report_manifest.json | wc -l`` **= 142 份 / 22 家**
    复算，结论不变：仍为 **21/22 家**取到窗口、**21/22 家**"报告期主序 ≠ ``created_at`` 倒序"。
    该聚合结论的两个端点由真实样本用例长期钉住
    （``test_real_sample_601138_selects_newest_report_period_not_newest_created_at`` /
    ``test_real_sample_002130_consistency_control_both_orders_agree``）；
    条件本身由 ``test_as_of_excludes_not_yet_ingested_manifest`` 钉住（``as_of`` 早于全部
    入库时刻 ⇒ ``no_report_before_as_of``，见下表）。

    | ``as_of`` | 语义 | 结果（601138，142 份数据下实测） |
    |---|---|---|
    | ``2026-08-20``（早于全部 ``created_at``） | as_of 时点仓库为空 | ``reason="no_report_before_as_of"``、0 段 |
    | ``2026-12-31``（≥ 全部 ``created_at``，等价实时口径） | 正常选取 | ``富士康…2026年半年度报告``、5 段 / 253 字符 |

    **仍不得**用 ``created_at`` 判"哪个报告更新"（主序恒为报告期）：``created_at`` 是解析
    入库时刻（同批 ingest 按目录序递增），与报告期无因果关系——实测 ``reports/工业富联(601138)``
    的 ``created_at`` 最新一份是 2023 年年度报告，而其报告期最新一份是 2026 年半年度报告。
    """
    if not as_of:
        return list(candidates)
    kept: list[dict[str, Any]] = []
    for item in candidates:
        created = str(item.get("created_at") or "")[:10]
        if created and created > as_of:
            continue  # as_of 时点尚未入库（前视）⇒ 不入选
        end = _period_end(str(item.get("report_period") or ""))
        if end:
            if end <= as_of:
                kept.append(item)
        elif created and created <= as_of:
            kept.append(item)
    return kept


def _resolve_source_path(manifest: dict[str, Any]) -> str:
    """定位全文副本：manifest 的 ``full_text_path`` 优先，缺失时回退报告目录下的 ``*.md``。"""
    raw = str(manifest.get("full_text_path") or "")
    if raw:
        path = Path(raw)
        if path.is_file():
            return str(path)
        # 绝对路径失效（换了机器/换了根目录）→ 用 manifest 所在目录做相对回退。
        manifest_path = str(manifest.get("_manifest_path") or "")
        if manifest_path:
            fallback = _first_markdown(Path(manifest_path).parent)
            if fallback:
                return fallback
        return ""
    manifest_path = str(manifest.get("_manifest_path") or "")
    if manifest_path:
        return _first_markdown(Path(manifest_path).parent)
    return ""


def _first_markdown(directory: Path) -> str:
    try:
        for path in sorted(directory.glob("*.md")):
            if path.is_file():
                return str(path)
    except Exception as exc:  # noqa: BLE001 - 目录不可读 → 视作缺失
        logger.warning("report_window: 枚举报告目录内 md 失败: %s", exc)
    return ""


@dataclass(frozen=True)
class _ParsedSection:
    """全文按标题切分后的原始段落（内部中间态，不入 ``ReportWindow``）。

    ``level`` = 标题层级（``#`` 个数）；``heading`` = **原始标题行**（保留 ``##``/``###``
    等原样写法，供子树重建时逐字还原源文本）。
    """

    title: str
    level: int
    heading: str
    body: str


def _split_sections(text: str) -> list[_ParsedSection]:
    """把全文按 Markdown 标题切为 (title, level, heading, body) 段（无标题的前导文本丢弃）。

    保留**空正文**的标题（父章节常无正文，只有子标题）——子树边界下这类父章节仍有
    **子树正文**（F8），故不能在此处按"自身正文为空"丢弃。
    """
    sections: list[_ParsedSection] = []
    current_title: str | None = None
    current_level = 0
    current_heading = ""
    body: list[str] = []

    def _flush() -> None:
        if current_title is not None:
            sections.append(
                _ParsedSection(
                    title=current_title,
                    level=current_level,
                    heading=current_heading,
                    body="\n".join(body).strip(),
                )
            )

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            _flush()
            current_title = match.group(2).strip()
            current_level = len(match.group(1))
            current_heading = line.strip()
            body = []
        elif current_title is not None:
            body.append(line)
    _flush()
    return sections


def _match_group(title: str) -> str:
    """按 :data:`SECTION_GROUPS` 顺序返回首个命中的组 key（未命中 → 空串）。"""
    for group, keywords in SECTION_GROUPS:
        if any(keyword in title for keyword in keywords):
            return group
    return ""


def _render(title: str, body: str) -> str:
    """章节文本渲染（标题行 + 正文），保证"证据来自哪一节"可追溯。"""
    return f"## {title}\n{body}".strip()


def _subtree_span(parsed: list[_ParsedSection], index: int) -> int:
    """子树**结束下标（左闭右开）**：命中章节的文本延伸到"下一个同级或更高级标题"为止。

    F8 边界口径：``parsed[index]`` 的子树 = 它自己 + 所有层级更深的后续段落，直到遇到
    ``level <= parsed[index].level`` 的标题（不含）。**确定性**：只依赖标题层级序列。
    """
    level = parsed[index].level
    end = index + 1
    while end < len(parsed) and parsed[end].level > level:
        end += 1
    return end


def _subtree_text(parsed: list[_ParsedSection], start: int, end: int) -> str:
    """按**原始标题行**逐段拼接子树文本（``[start, end)``），保留源文档的标题写法。"""
    chunks: list[str] = []
    for section in parsed[start:end]:
        chunks.append(section.heading if not section.body else f"{section.heading}\n{section.body}")
    return "\n".join(chunks).strip()


def _claim_roots(parsed: list[_ParsedSection]) -> list[tuple[int, int, str]]:
    """**顶层声明制**（F8 裁定 A）：自上而下遍历标题树，产出被认领的 ``(start, end, group)``。

    规则（裁定 A 原文）：

    1. 自上而下遍历；某节点标题命中白名单即**认领其整棵子树**（到下一个同级或更高级标题
       为止），并**不再向内层继续认领**——子树内其他命中标题（例如 ``mda`` 父章节里的
       "风险"子标题）**不产生第二次认领**；
    2. 该子树文本归属**父章节（认领者）所在的组**；
    3. 因此**一条文本只会入窗一次**，不会出现"父章节与子标题都入窗"的重复计字符。

    返回顺序 = **文档顺序**；组间顺序由 :func:`_select_sections` 施加（``mda``→``business``→``risk``）。
    自身与整棵子树都没有正文的纯空标题**不构成认领**（其内层仍可继续被认领）。
    """
    roots: list[tuple[int, int, str]] = []
    blocked_until = 0  # 已被某个认领子树覆盖到的下标（左闭右开的右端）
    for index, parsed_section in enumerate(parsed):
        if index < blocked_until:
            continue  # 落在已被认领的子树内 ⇒ 不再向内层认领（裁定 A-1）
        group = _match_group(parsed_section.title)
        if not group:
            continue
        start, end = index, _subtree_span(parsed, index)
        if not any(section.body for section in parsed[start:end]):
            continue  # 纯空标题（无正文且无子树正文）⇒ 不认领，内层继续
        roots.append((start, end, group))
        blocked_until = end
    return roots


def _select_sections(text: str, *, max_chars: int, max_sections: int) -> tuple[list[WindowSection], int, bool]:
    """选择被认领的叙事章节子树（子树边界 + 顶层声明制 + **按命中组等额分配**，F8）。

    * 认领：见 :func:`_claim_roots`（自上而下、祖先认领整棵子树、内层不再认领 ⇒ 无重复）；
    * 组间有序：``mda`` → ``business`` → ``risk``（组内保持文档顺序）；
    * **``max_sections`` 计"被认领的章节数"**（不是标题数）：一棵巨大子树只算 1 段；
    * **预算分配（裁定 C）= 按"有内容的命中组"等额切分 + 余额顺延**：

      1. 命中组集合 = 本次实际认领到 ≥1 个章节的组（:data:`SECTION_GROUPS` 的子集，按组序）；
      2. 每组初始份额 = ``max_chars // 命中组数``（整除余数同样顺延，**不引入任何魔法常量**）；
      3. 组内按文档序取章节，每段截到**该组当前可用额度**；
      4. 某组未用完的余额**顺延**给下一个命中组 ⇒ 内容很少的组（如 ``risk`` 仅 55 字符）
         不会浪费预算；
      5. 总消耗恒 ≤ ``max_chars``（**总硬上限性质不变**）。

    **为什么需要这条规则（实测依据）**：早前"按组序顺序吃满总预算"的实现在子树边界下
    **坍缩**——``mda`` 的首棵子树（如 ``第三节 管理层讨论与分析``，实测 47,810 字符）一票
    吃满 12,000 ⇒ 20/21 家只剩 1 段、``risk`` 组 **100% 拿不到内容**（它永远排在最后），
    三组白名单退化为"只有 mda"。等额分配后三组都能拿到内容。
    **取舍（显式披露）**：大子树会被截到**其组份额**（单段不再是完整子树）。

    Returns:
        ``(sections, chars, truncated)``：``chars`` 为选中文本总字符数（恒 ≤ ``max_chars``）；
        ``truncated`` 表示"因预算未能完整保留"（截断过任何一章，或某组额度用尽而仍有候选，
        或段数上限已到而仍有候选）。
    """
    parsed = _split_sections(text)
    roots = _claim_roots(parsed)
    hit_groups = [group for group, _ in SECTION_GROUPS if any(root[2] == group for root in roots)]
    if not hit_groups:
        return [], 0, False

    share, remainder = divmod(max_chars, len(hit_groups))

    selected: list[WindowSection] = []
    used = 0
    truncated = False
    quota = 0  # 本组可用额度（自身份额 + 前序组顺延余额）
    stop = False
    for position, group in enumerate(hit_groups):  # 组间有序：先 mda，再 business，最后 risk
        # 本组额度 = 自身份额（首个命中组额外承接整除余数）+ 前序组未用完的顺延余额
        quota += share + (remainder if position == 0 else 0)
        spent = 0
        for start, end, _root_group in [root for root in roots if root[2] == group]:
            if len(selected) >= max_sections:
                truncated = True
                stop = True
                break
            room = quota - spent
            if room <= 0:
                truncated = True
                break  # 本组额度用尽 ⇒ 余额顺延给下一组
            candidate_text = _subtree_text(parsed, start, end)
            if not candidate_text:
                continue
            if len(candidate_text) > room:
                # 超出本组额度 → 截断到额度上界（"单段不再是完整子树"，见 docstring 取舍）
                selected.append(
                    WindowSection(
                        title=parsed[start].title,
                        text=candidate_text[:room],
                        chars=room,
                        group=group,
                    )
                )
                spent += room
                used += room
                truncated = True
                break
            selected.append(
                WindowSection(
                    title=parsed[start].title,
                    text=candidate_text,
                    chars=len(candidate_text),
                    group=group,
                )
            )
            spent += len(candidate_text)
            used += len(candidate_text)
        quota -= spent  # 未用完的余额顺延给下一个命中组
        if stop:
            break
    return selected, used, truncated


def select_report_window(
    symbol: str,
    *,
    as_of: str | None = None,
    reports_root_path: str | Path | None = None,
    max_chars: int | None = None,
    max_sections: int | None = None,
    enabled: bool = True,
) -> ReportWindow:
    """从**本地已解析**的财报中选出叙事章节窗口（纯规则、禁 LLM、无网络、不抛异常）。

    步骤：
    1. glob ``<reports_root>/*(<6位代码>)/财报/<报告期>/.report_manifest.json`` → 解析 manifest；
    2. 若给 ``as_of``：只取**报告期期末日与入库时刻都不晚于 ``as_of``** 的报告；按**报告期**
       倒序取**最新一期**（``created_at`` 仅作 tie-break）—— 口径与偏差理由见模块 docstring
       「序口径」；
    3. 读 manifest 的 ``full_text_path``（``reports_full`` 全文副本；缺失则回退报告目录下的
       ``*.md``）；
    4. 按 :data:`_HEADING_RE` 把全文切为 (title, body) 段（**边界 = 到下一个标题为止**）；
    5. 依 :data:`SECTION_GROUPS` 顺序选段（**子串**匹配），每段截断到剩余预算，累计到
       ``max_chars`` / ``max_sections``（两者均为**硬上限**）；
    6. 任何一步失败 → 返回带 ``reason`` 的空窗口。

    Args:
        symbol: 标的（``600519.SH`` / ``300750`` 均可；按 6 位代码匹配目录名）。
        as_of: 截止日期 ``YYYY-MM-DD``；**报告期期末日与入库时刻都不晚于**该日期的报告才
            可入选（F5 前视消除，见 :func:`_manifests_before`）；``None``/空串 = 不做时间
            过滤（取报告期最新一期）。
        reports_root_path: 报告根目录；``None`` = 配置 ``report_window.reports_root``，再缺省
            ``report_parser.reports_root()``（``<PROJECT_ROOT>/reports``）。
        max_chars: 窗口字符预算；``None`` = 配置 ``report_window.max_chars``，
            再缺省 §15.1-F 默认 :data:`_DEFAULT_MAX_CHARS`。
        max_sections: 窗口段数预算；``None`` = 配置 ``report_window.max_sections``，
            再缺省 :data:`_DEFAULT_MAX_SECTIONS`。
        enabled: 传 ``False`` 直接返回 ``disabled`` 空窗口（**不读配置**，便于测试显式控制）；
            调用方若想让配置决定，用 :func:`report_window_enabled`（节点即如此）。

    Returns:
        :class:`ReportWindow`；``reason`` 非空表示未取到（**调用方负责显式记账**）。
    """
    window = ReportWindow(symbol=symbol or "")
    if not enabled:
        window.reason = "disabled"
        return window

    section = _settings()
    if max_chars is None:
        max_chars = getattr(section, "max_chars", None) or _DEFAULT_MAX_CHARS
    if max_sections is None:
        max_sections = getattr(section, "max_sections", None) or _DEFAULT_MAX_SECTIONS
    if reports_root_path is None:
        reports_root_path = getattr(section, "reports_root", None) or None

    try:
        code = _symbol_code(symbol or "")
        if not code:
            window.reason = "no_local_report"
            return window

        root = Path(reports_root_path).expanduser() if reports_root_path else reports_root()
        candidates, matched_dir = _manifest_candidates(root, code)
        if not candidates:
            window.reason = "manifest_unreadable" if matched_dir else "no_local_report"
            return window

        candidates = _manifests_before(candidates, as_of)
        if not candidates:
            window.reason = "no_report_before_as_of"
            return window

        manifest = candidates[0]
        window.report_name = str(manifest.get("report_name") or "")
        window.report_period = str(manifest.get("report_period") or "")
        page_count = manifest.get("page_count")
        window.page_count = page_count if isinstance(page_count, int) else None

        source_path = _resolve_source_path(manifest)
        if not source_path:
            window.reason = "full_text_missing"
            return window
        window.source_path = source_path

        text = Path(source_path).read_text(encoding="utf-8")

        sections, chars, truncated = _select_sections(text, max_chars=max_chars, max_sections=max_sections)
        if not sections:
            window.reason = "no_matching_section"
            return window

        window.sections = sections
        window.chars = chars
        window.truncated = truncated
        return window
    except Exception as exc:  # noqa: BLE001 - fail-open：任何异常只 warning，绝不抛出
        logger.warning("report_window: 选取财报原文窗口失败（fail-open）: %s", exc)
        window.sections = []
        window.chars = 0
        window.truncated = False
        window.reason = f"internal_error: {exc}"
        return window
