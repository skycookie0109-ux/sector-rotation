"""美股類股輪動計分，輸出 web/data/dashboard_us.json。

沿用台股那套標準化與加權邏輯（robust_z / pct_rank / RRG 都直接 import），
差別在資料來源與可用指標：美股這邊沒有等同於三大法人的公開日資料，
所以沒有籌碼面；估值也先留白（需要市值，SEC frames 不提供）。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import us  # noqa: E402
from build import excess_return, pct_rank, quadrant, robust_z, rrg, verdict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "data"
OUT.mkdir(parents=True, exist_ok=True)

METRICS = {
    "rev_yoy":        ("營收年增率", 1, "fundamental"),
    "net_margin":     ("稅後淨利率", 1, "fundamental"),
    "roe":            ("股東權益報酬率(年化)", 1, "fundamental"),
    "profit_breadth": ("獲利家數占比", 1, "fundamental"),
    "rs_ratio":       ("相對強度 RS-Ratio", 1, "technical"),
    "rs_momentum":    ("相對強度動能 RS-Momentum", 1, "technical"),
    "excess_20":      ("20日超額報酬", 1, "technical"),
    "excess_60":      ("60日超額報酬", 1, "technical"),
    "excess_120":     ("120日超額報酬", 1, "technical"),
}

HORIZONS = {
    "short": {
        "label": "短期", "range": "1～3 個月",
        "note": "這段期間沒有新的財報資訊。美股沒有等同台股三大法人的公開日資料，"
                "所以短期完全由價格動能構成——這一頁請當成「市場現在在買什麼」，"
                "不是「哪個產業體質好」。",
        "weights": {"rs_momentum": 0.45, "excess_20": 0.35, "rev_yoy": 0.20},
    },
    "medium": {
        "label": "中期", "range": "3 個月～2 年",
        "note": "基本面主導。用季報的營收年增與獲利品質判斷產業景氣位置，"
                "技術面只作為「市場是否已經認同」的驗證。",
        "weights": {"rev_yoy": 0.26, "net_margin": 0.16, "roe": 0.18,
                    "profit_breadth": 0.10, "rs_ratio": 0.18, "excess_60": 0.12},
    },
    "long": {
        "label": "長期", "range": "2 年以上",
        "note": "只看產業體質。刻意不納入短期技術面，因為那些訊號在兩年尺度上"
                "沒有預測價值。",
        "weights": {"roe": 0.34, "net_margin": 0.26, "rev_yoy": 0.18,
                    "profit_breadth": 0.14, "excess_120": 0.08},
    },
}


def pick_periods() -> tuple[str, str, str, str]:
    """挑最近一期有資料的季度，回傳 (本期, 去年同期, 本期期末, 季別標籤)。"""
    import datetime as dt

    today = dt.date.today()
    for back in range(0, 6):
        month = today.month - 3 * back
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        q = (month - 1) // 3 + 1
        cur = f"CY{year}Q{q}"
        if len(us.merged_frame(us.REVENUE_TAGS, cur)) > 300:
            return cur, f"CY{year - 1}Q{q}", f"{cur}I", f"{year} Q{q}"
    raise SystemExit("找不到有足夠資料的季度")


def _write_stock_index(sic_map: dict) -> None:
    """輸出「美股代號 -> 類股」的反查表。

    收錄範圍是「有 SIC 分類且有股票代號」的公司，而不是「本季財報有進榜」的
    公司。這個差別很重要：SEC 的 frames API 只收期間剛好對齊曆年季度的資料，
    所以像 NVIDIA（財年一月底結束）、Walmart 這種非曆年財年的公司會落榜。
    反查表要回答的是「這檔屬於哪一類」，跟它這季有沒有對齊曆年無關。
    """
    tickers = us.company_tickers()
    industries = sorted(set(us.SECTOR_ETF))
    idx = {name: i for i, name in enumerate(industries)}

    rows = []
    for cik, (ticker, title) in tickers.items():
        info = sic_map.get(cik)
        sector = us.sic_to_sector(info[1] if info else None, cik)
        if sector is None:
            continue
        rows.append([ticker, title[:44], idx[sector], 0])
    rows.sort()

    path = OUT / "stocks_us.json"
    path.write_text(
        json.dumps({"industries": industries, "markets": ["美股"],
                    "stocks": rows}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print(f"輸出 {path.name}  {len(rows):,} 檔  ({path.stat().st_size / 1024:.0f} KB)")


def main() -> None:
    print("=" * 68)
    print("美股類股輪動")
    print("=" * 68)

    print("\n[價格] Yahoo Finance")
    prices = us.fetch_prices()
    bench = dict(prices.get("__BENCH__", []))
    if not bench:
        raise SystemExit("抓不到 SPY 基準價格")

    print("\n[季度] 尋找最近一期 SEC 資料")
    cur_p, prev_p, inst_p, label = pick_periods()
    print(f"  本期={cur_p}  去年同期={prev_p}  期末={inst_p}")

    print("\n[基本面] SEC XBRL")
    rev_now = us.merged_frame(us.REVENUE_TAGS, cur_p)
    rev_prev = us.merged_frame(us.REVENUE_TAGS, prev_p)
    net_now = us.merged_frame(["NetIncomeLoss"], cur_p)
    equity = us.merged_frame(["StockholdersEquity"], inst_p)
    print(f"  營收本期={len(rev_now):,} 去年同期={len(rev_prev):,} "
          f"淨利={len(net_now):,} 權益={len(equity):,}")

    # DERA 的季度批次檔會比 XBRL frames 晚幾週才發布，往前退到抓得到的那一期。
    # SIC 分類幾乎不會變動，用上一季的對照表不影響結果。
    year, q = int(cur_p[2:6]), int(cur_p[-1])
    for _ in range(3):     # 最新一季通常還沒發布，先往前找到起點
        try:
            us.load_sic_map(f"{year}q{q}")
            break
        except Exception:  # noqa: BLE001
            print(f"  {year}q{q} 尚未發布，往前一季")
            q -= 1
            if q == 0:
                year, q = year - 1, 4
    sic_map = us.load_sic_map_multi(f"{year}q{q}", back=4)
    if not sic_map:
        raise SystemExit("抓不到 SEC 的 SIC 對照表")
    print(f"  SIC 對照表合計 {len(sic_map):,} 家")

    # --- 依 SIC 歸類後彙總
    agg: dict[str, dict] = defaultdict(
        lambda: {"rev": 0.0, "rev_prev": 0.0, "net": 0.0, "equity": 0.0,
                 "n": 0, "profitable": 0, "n_net": 0})
    unclassified = 0
    for cik, rev in rev_now.items():
        info = sic_map.get(cik)
        sector = us.sic_to_sector(info[1] if info else None, cik)
        if sector is None:
            unclassified += 1
            continue
        prev = rev_prev.get(cik)
        if prev is None or prev <= 0 or rev <= 0:
            continue
        b = agg[sector]
        b["rev"] += rev
        b["rev_prev"] += prev
        b["n"] += 1
        net = net_now.get(cik)
        if net is not None:
            b["net"] += net
            b["n_net"] += 1
            if net > 0:
                b["profitable"] += 1
        eq = equity.get(cik)
        if eq and eq > 0:
            b["equity"] += eq
    print(f"  歸類成功 {sum(v['n'] for v in agg.values()):,} 家、"
          f"SIC 對不到類股 {unclassified:,} 家")

    # --- 組出每個類股的原始指標
    sectors: dict[str, dict] = {}
    for name, etf in us.SECTOR_ETF.items():
        b = agg.get(name, {})
        raw: dict[str, float | None] = {}
        if b.get("rev_prev"):
            raw["rev_yoy"] = round(100 * (b["rev"] / b["rev_prev"] - 1), 2)
        else:
            raw["rev_yoy"] = None
        raw["net_margin"] = (round(100 * b["net"] / b["rev"], 2)
                             if b.get("rev") else None)
        # SEC 的 CY####Q# 是單季區間（不像台股季報是累計），所以年化就是乘 4
        raw["roe"] = (round(100 * b["net"] * 4 / b["equity"], 2)
                      if b.get("equity") else None)
        raw["profit_breadth"] = (round(100 * b["profitable"] / b["n_net"], 1)
                                 if b.get("n_net") else None)

        series = prices.get(name, [])
        rrg_data = None
        if series:
            sec_s, ben_s, day_s = [], [], []
            for day, close in series:
                if day in bench:
                    sec_s.append(close)
                    ben_s.append(bench[day])
                    day_s.append(day)
            rrg_data = rrg(sec_s, ben_s, w=60, dates=day_s)
            if rrg_data:
                raw["rs_ratio"] = rrg_data["rs_ratio"]
                raw["rs_momentum"] = rrg_data["rs_momentum"]
            for n in (20, 60, 120):
                raw[f"excess_{n}"] = excess_return(sec_s, ben_s, n)

        sectors[name] = {
            "industry": name, "etf": etf, "members": b.get("n", 0),
            # 產業規模，時間軸圖的圓圈大小。單位換成十億美元。
            "scale": round(b["rev"] / 1e9, 1) if b.get("rev") else None,
            "raw": raw, "rrg": rrg_data,
        }

    # --- 標準化與加權（與台股同一套）
    keys = list(sectors)
    z: dict[str, dict] = {}
    for metric, (_, direction, _) in METRICS.items():
        vals = {k: sectors[k]["raw"].get(metric) for k in keys}
        for k, v in robust_z(vals).items():
            z.setdefault(k, {})[metric] = None if v is None else v * direction

    for hname, cfg in HORIZONS.items():
        total_weight = sum(cfg["weights"].values())
        composite = {}
        for k in keys:
            total, used, contrib = 0.0, 0.0, []
            for metric, w in cfg["weights"].items():
                zv = z[k].get(metric)
                if zv is None:
                    continue
                total += zv * w
                used += w
                contrib.append([metric, round(zv, 2)])
            composite[k] = total / total_weight if total_weight else 0.0
            contrib.sort(key=lambda c: -abs(c[1] * cfg["weights"][c[0]]))
            sectors[k].setdefault("horizons", {})[hname] = {
                "raw_score": round(composite[k], 3),
                "coverage": round(100 * used / total_weight, 0),
                "contributions": contrib,
            }
        ranks = pct_rank(composite)
        for pos, k in enumerate(sorted(keys, key=lambda x: -composite[x]), 1):
            sectors[k]["horizons"][hname]["score"] = ranks[k]
            sectors[k]["horizons"][hname]["rank"] = pos

    last_day = max(bench)
    payload = {
        "market": "us",
        "generated_at": last_day,
        "data_asof": {"price": last_day, "financials": label,
                      "history_days": len(bench)},
        "horizons": HORIZONS,
        "metric_meta": {k: {"label": v[0], "direction": v[1], "group": v[2]}
                        for k, v in METRICS.items()},
        "sectors": [],
    }
    for name, s in sorted(sectors.items(),
                          key=lambda kv: -kv[1]["horizons"]["medium"]["score"]):
        r = s["rrg"]
        payload["sectors"].append({
            "industry": name, "etf": s["etf"], "members": s["members"],
            "members_twse": s["members"], "members_otc": 0,
            "scale": s["scale"], "weak_quarterly": False,
            "raw": s["raw"], "rrg": r,
            "quadrant": quadrant(r["rs_ratio"], r["rs_momentum"]) if r else None,
            "scores": {h: {**s["horizons"][h],
                           "verdict": verdict(s["horizons"][h]["score"])}
                       for h in HORIZONS},
            "top_stocks": [],
        })

    path = OUT / "dashboard_us.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    _write_stock_index(sic_map)
    print(f"\n輸出 {path}  ({path.stat().st_size / 1024:.0f} KB)")
    print("\n中期（基本面主導）排名：")
    for s in payload["sectors"]:
        m = s["scores"]["medium"]
        print(f"  {m['rank']:2d}. {s['industry']:8s}({s['etf']:4s}) {m['score']:5.1f} 分"
              f"  {m['verdict']:3s}  營收YoY={s['raw'].get('rev_yoy')}%"
              f"  淨利率={s['raw'].get('net_margin')}%  {s['members']} 家")


if __name__ == "__main__":
    main()
