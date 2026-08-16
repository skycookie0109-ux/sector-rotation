"""抓取最新一期的全市場快照，存到 data/snapshot/。

這些端點都只回傳「最新一期」，所以每次執行就是覆蓋更新。
歷史序列由 backfill.py 負責。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tpex  # noqa: E402
import twse  # noqa: E402
from sectors import normalize_index_name, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
SNAP.mkdir(parents=True, exist_ok=True)

# 名稱 -> openapi 路徑
ENDPOINTS = {
    "monthly_revenue": "opendata/t187ap05_L",       # 月營收（自帶去年同月）
    "income_general": "opendata/t187ap06_L_ci",     # 綜合損益表 一般業
    "income_finance": "opendata/t187ap06_L_basi",   # 綜合損益表 金融業
    "income_holding": "opendata/t187ap06_L_fh",     # 綜合損益表 金控業
    "income_insurance": "opendata/t187ap06_L_ins",  # 綜合損益表 保險業
    "income_broker": "opendata/t187ap06_L_bd",      # 綜合損益表 證券期貨業
    "income_other": "opendata/t187ap06_L_mim",      # 綜合損益表 異業
    "balance_general": "opendata/t187ap07_L_ci",    # 資產負債表 一般業
    "balance_finance": "opendata/t187ap07_L_basi",
    "balance_holding": "opendata/t187ap07_L_fh",
    "balance_insurance": "opendata/t187ap07_L_ins",
    "balance_broker": "opendata/t187ap07_L_bd",
    "balance_other": "opendata/t187ap07_L_mim",
    "industry_eps": "opendata/t187ap14_L",          # 各產業 EPS 統計
    "valuation": "exchangeReport/BWIBBU_ALL",       # PE / 殖利率 / PB
    "daily_quote": "exchangeReport/STOCK_DAY_ALL",  # 個股日成交
    "margin": "exchangeReport/MI_MARGN",            # 融資融券餘額
    "index_snapshot": "exchangeReport/MI_INDEX",    # 各類指數當日
    "company": "opendata/t187ap03_L",               # 公司基本資料
}


# 上櫃（櫃買 OpenAPI）。同樣只回傳最新一期。
TPEX_ENDPOINTS = {
    "otc_valuation": "tpex_mainboard_peratio_analysis",  # PE / 殖利率 / PB
    "otc_quote": "tpex_mainboard_daily_close_quotes",    # 收盤行情（含股本）
    "otc_margin": "tpex_mainboard_margin_balance",       # 融資融券
    "otc_qfii_industry": "tpex_3insti_qfii_industry",    # 外資類股持股比率
}


def main(refresh: bool = True) -> None:
    print("=" * 68)
    print("抓取全市場快照")
    print("=" * 68)

    print("\n[上市 TWSE]")
    for name, path in ENDPOINTS.items():
        try:
            data = twse.openapi(path, cache=not refresh)
            (SNAP / f"{name}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  [OK]   {name:20s} {len(data):>6,} 筆")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name:20s} {type(exc).__name__}: {str(exc)[:70]}")

    print("\n[上櫃 TPEx]")
    for name, path in TPEX_ENDPOINTS.items():
        try:
            data = tpex.api(path, cache=not refresh)
            (SNAP / f"{name}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  [OK]   {name:20s} {len(data):>6,} 筆")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name:20s} {type(exc).__name__}: {str(exc)[:70]}")

    # 上櫃月營收在櫃買 API 沒有，要從公開資訊觀測站的 HTML 表格解析
    try:
        period = tpex.latest_revenue_period("otc")
        if period is None:
            raise RuntimeError("找不到最近一期上櫃月營收")
        rows = tpex.fetch_monthly_revenue(*period, market="otc")
        (SNAP / "otc_monthly_revenue.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"  [OK]   {'otc_monthly_revenue':20s} {len(rows):>6,} 筆"
              f"  (民國{period[0]}年{period[1]}月)")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {'otc_monthly_revenue':20s} {type(exc).__name__}: {str(exc)[:70]}")

    _report_sector_coverage()


def _report_sector_coverage() -> None:
    """檢查產業別 <-> 類指數的對應是否完整，缺的要讓使用者看到。"""
    rev = json.loads((SNAP / "monthly_revenue.json").read_text(encoding="utf-8"))
    idx = json.loads((SNAP / "index_snapshot.json").read_text(encoding="utf-8"))

    index_names = {normalize_index_name(r["指數"]) for r in idx}
    industries: dict[str, int] = {}
    for row in rev:
        industries[row.get("產業別", "?")] = industries.get(row.get("產業別", "?"), 0) + 1

    print()
    print("=" * 68)
    print("類股對應檢查")
    print("=" * 68)
    matched, unmatched = [], []
    for ind, n in sorted(industries.items(), key=lambda kv: -kv[1]):
        hit = resolve(ind, index_names)
        (matched if hit else unmatched).append((ind, n, hit))

    for ind, n, hit in matched:
        print(f"  [有指數] {ind:14s} {n:4d} 家  ->  {hit}")
    for ind, n, _ in unmatched:
        print(f"  [無指數] {ind:14s} {n:4d} 家  （僅基本面/籌碼面，無技術面）")
    print(f"\n  合計 {len(matched)} 個產業有類指數、{len(unmatched)} 個沒有")


if __name__ == "__main__":
    main(refresh="--cached" not in sys.argv)
