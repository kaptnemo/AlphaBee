"""同行业对标候选闭集测试（对标组在线兜底，REPORT_QUALITY_FIX_ROADMAP §11 P2-①）。"""

from __future__ import annotations

from alphabee.company_track.peer_universe import build_peer_universe


def test_build_peer_universe_maps_names_and_excludes_self():
    universe = build_peer_universe(["300223.SZ", "688766.SH", "300223.SZ"], exclude="300223.SZ")

    # 去重 + 保序 + 排除自身
    assert [item["code"] for item in universe] == ["688766.SH"]
    # 名称来自静态股票表（非空即证明映射链路可用；普冉股份）
    assert universe[0]["name"] == "普冉股份"


def test_build_peer_universe_name_missing_falls_back_empty():
    universe = build_peer_universe(["999999.SZ"])
    assert universe == [{"code": "999999.SZ", "name": ""}]


def test_build_peer_universe_respects_limit():
    codes = ["300223.SZ", "688766.SH", "300782.SZ"]
    universe = build_peer_universe(codes, limit=2)
    assert [item["code"] for item in universe] == ["300223.SZ", "688766.SH"]


def test_build_peer_universe_lowercase_normalized():
    universe = build_peer_universe(["300223.sz"])
    assert universe[0]["code"] == "300223.SZ"
