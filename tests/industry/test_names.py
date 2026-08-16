"""行业名规范字典单元测试（industry-context Phase 2，B1 优化）。"""

from alphabee.industry.names import (
    EXTRACTION_HINTS,
    catalog,
    group_keys,
    industry_display_name,
    industry_in_group,
    industry_keys_for_name,
    keyword_extract_industry,
    normalize_name,
)

# ── 目录完整性 ─────────────────────────────────────────────────────────────


def test_catalog_has_all_l1_industries():
    names = catalog()
    l1 = {key: value for key, value in names.items() if key.startswith("sw_l1:")}
    assert len(l1) == 31  # 申万一级 31 个
    # 抽查关键行业代码（与 index_classify 实测对齐）
    assert l1["sw_l1:801780.SI"] == "银行"
    assert l1["sw_l1:801080.SI"] == "电子"
    assert l1["sw_l1:801180.SI"] == "房地产"
    assert l1["sw_l1:801150.SI"] == "医药生物"
    assert l1["sw_l1:801770.SI"] == "通信"


def test_catalog_has_curated_l2():
    names = catalog()
    assert names["sw_l2:801081.SI"] == "半导体"
    assert names["sw_l2:801193.SI"] == "证券Ⅱ"
    assert names["sw_l2:801194.SI"] == "保险Ⅱ"


def test_industry_display_name_lookup():
    assert industry_display_name("sw_l1:801780.SI") == "银行"
    # 未知 key 回退 code 部分
    assert industry_display_name("sw_l1:999999.SI") == "999999.SI"
    assert industry_display_name("sw_l2:801081.SI") == "半导体"


# ── 名称归一 ───────────────────────────────────────────────────────────────


def test_normalize_name_aliases_and_suffix():
    assert normalize_name("芯片") == "半导体"  # 别名
    assert normalize_name("证券Ⅱ") == "证券"  # 罗马后缀
    assert normalize_name("券商") == "证券"  # 别名 + 后缀
    assert normalize_name("银行") == "银行"
    assert normalize_name("") == ""


# ── 名称 → 行业 key ────────────────────────────────────────────────────────


def test_industry_keys_for_name():
    assert industry_keys_for_name("银行") == ["sw_l1:801780.SI"]
    assert industry_keys_for_name("半导体") == ["sw_l2:801081.SI"]
    assert industry_keys_for_name("证券") == ["sw_l2:801193.SI"]  # 证券Ⅱ 后缀宽容
    assert industry_keys_for_name("芯片") == ["sw_l2:801081.SI"]  # 别名
    assert industry_keys_for_name("白酒") == ["sw_l2:801125.SI"]
    assert industry_keys_for_name("量子计算") == []
    assert industry_keys_for_name("") == []


# ── 行业组归属（迁移自 thesis 硬编码集合）──────────────────────────────────


def test_financial_group_membership():
    assert industry_in_group("银行", "financial")
    assert industry_in_group("证券", "financial")
    assert industry_in_group("保险", "financial")
    assert not industry_in_group("白酒", "financial")
    assert not industry_in_group("", "financial")
    assert not industry_in_group("量子计算", "financial")


def test_high_leverage_group_membership():
    assert industry_in_group("银行", "high_leverage")
    assert industry_in_group("房地产", "high_leverage")
    assert industry_in_group("建筑装饰", "high_leverage")
    assert not industry_in_group("半导体", "high_leverage")


def test_high_rd_group_membership():
    assert industry_in_group("半导体", "high_rd")
    assert industry_in_group("芯片", "high_rd")  # 别名
    assert industry_in_group("医药", "high_rd")  # 别名 → 医药生物
    assert industry_in_group("计算机", "high_rd")
    assert industry_in_group("电子", "high_rd")
    assert not industry_in_group("银行", "high_rd")


def test_unknown_group_name_is_empty():
    assert group_keys("not_a_group") == set()


# ── 文本兜底抽取（迁移自 company_context）──────────────────────────────────


def test_keyword_extract_industry():
    assert keyword_extract_industry("公司主营芯片设计制造") == "半导体"
    assert keyword_extract_industry("银行板块今日走强") == "银行"
    assert keyword_extract_industry("") == ""
    assert keyword_extract_industry("这是无关文本") == ""


def test_extraction_hints_order_sensitive():
    # 顺序敏感：先命中先返回
    assert keyword_extract_industry("食品饮料公司发布财报") == "食品饮料"
    assert EXTRACTION_HINTS[0] == ("白酒", "白酒")
