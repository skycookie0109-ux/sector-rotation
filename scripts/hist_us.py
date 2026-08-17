"""回補美股各類股的歷史財報，讓水準型指標也能跟自己的歷史比。

台股那邊已經把營益率、ROE、本益比改成「相對自身歷史」，理由是絕對值高低
主要反映商業模式而非景氣。美股一樣有這個問題——資訊科技的淨利率天生就比
必需消費高好幾倍，跨產業排名等於在獎勵產業屬性。

資料來自 SEC 的 XBRL frames API：一次請求就能拿到「某一科目、某一期間、
全體申報公司」的數字，所以 8 年份也只要一百多次請求。

一樣要處理兩件事：
1. frames 的季度資料本來就是單季（不像台灣是累計），所以不需要還原，
   但要注意 CY2025Q4 這種代碼指的是日曆季，跟公司自己的會計年度未必一致，
   SEC 只會回傳期間確實對齊的公司。
2. 單季有季節性，所以滾成 TTM 再比。
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import us  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "history"
HIST.mkdir(parents=True, exist_ok=True)
OUT = HIST / "fin_history_us.csv"

OP_TAGS = ["OperatingIncomeLoss"]
NI_TAGS = ["NetIncomeLoss", "ProfitLoss"]
EQ_TAGS = ["StockholdersEquity",
           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]


def calendar_quarters(n: int) -> list[tuple[int, int]]:
    """從最近一個「大致上已經申報完」的日曆季往回列 n 季。"""
    import datetime as _dt
    today = _dt.date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    # 財報大約落後一季半才會申報齊全，往回退兩季比較保險
    for _ in range(2):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return list(reversed(out))


def main(n_quarters: int = 32, refresh: bool = False) -> None:
    quarters = calendar_quarters(n_quarters)
    latest = quarters[-1]

    if OUT.exists() and not refresh:
        with OUT.open(encoding="utf-8") as fh:
            have = {r["quarter"] for r in csv.DictReader(fh)}
        if f"{latest[0]}Q{latest[1]}" in have:
            print(f"美股歷史財報已是最新（{latest[0]}Q{latest[1]}），跳過")
            return

    print("=" * 68)
    print(f"回補美股歷史財報：{quarters[0][0]}Q{quarters[0][1]} ~ "
          f"{latest[0]}Q{latest[1]}（{len(quarters)} 季）")
    print("=" * 68)

    # SIC 產業歸類。合併最近幾季的申報，涵蓋率比單季高很多。
    sic_map = us.load_sic_map_multi(f"{latest[0]}q{latest[1]}", back=4)
    print(f"  SIC 對照 {len(sic_map):,} 家")

    # 逐公司逐季存起來，不先彙總。
    #
    # 原因：多數公司的第四季數字併在年報裡，不會單獨申報，所以 SEC 的
    # CY{年}Q4 只有八百多家、其他季有三千九百多家。如果每季各自彙總再相加，
    # TTM 視窗裡的成分股會季季不同，算出來的利潤率是在比較不同的公司組合。
    # 改成只採「四季都有申報」的公司，每個 TTM 值就是內部一致的。
    # 逐「會計科目標籤」取數，最後才合併。
    #
    # 這一步必須逐標籤做，不能先合併再相減。第四季多數公司併在年報裡不單獨
    # 申報，要用「年度 − Q1 − Q2 − Q3」回推；如果年度那筆挑到的是 Revenues、
    # 季度那筆挑到的是 RevenueFromContractWithCustomer...，兩者定義不同，相減
    # 出來的第四季會是垃圾（實測會產生整個類股淨利率 221% 這種不可能的值）。
    FIELDS = {"rev": (us.REVENUE_TAGS, False), "op": (OP_TAGS, False),
              "ni": (NI_TAGS, False), "eq": (EQ_TAGS, True)}
    raw: dict[tuple[int, int], dict[str, dict[int, float]]] = {
        k: {f: {} for f in FIELDS} for k in quarters}
    years = sorted({y for y, q in quarters if q == 4})

    for field, (tags, instant) in FIELDS.items():
        for tag in tags:
            per: dict[tuple[int, int], dict[int, float]] = {}
            for y, q in quarters:
                suffix = "I" if instant else ""
                per[(y, q)] = {r["cik"]: float(r["val"])
                               for r in us.frames(tag, f"CY{y}Q{q}{suffix}")
                               if r.get("cik") is not None}
            # 用同一個標籤的年度數字回推第四季
            if not instant:
                for y in years:
                    if (y, 4) not in per:
                        continue
                    whole = {r["cik"]: float(r["val"])
                             for r in us.frames(tag, f"CY{y}")
                             if r.get("cik") is not None}
                    for cik, total in whole.items():
                        if cik in per[(y, 4)]:
                            continue
                        parts = [per.get((y, i), {}).get(cik) for i in (1, 2, 3)]
                        if any(v is None for v in parts):
                            continue
                        val = total - sum(parts)
                        # 單季不該是負營收，也不該超過全年的六成
                        if field == "rev" and not (0 < val < total * 0.6):
                            continue
                        per[(y, 4)][cik] = val
            # 先命中的標籤優先，後面的標籤只補沒有的公司
            for k, vals in per.items():
                slot = raw[k][field]
                for cik, v in vals.items():
                    slot.setdefault(cik, v)

    for y, q in quarters:
        f = raw[(y, q)]
        print(f"  [OK]   {y}Q{q}  營收 {len(f['rev']):,} / 淨利 {len(f['ni']):,} 家")

    per_q: dict[tuple[int, int], dict[int, dict]] = {}
    for (y, q), f in raw.items():
        byc: dict[int, dict] = {}
        for cik, r in f["rev"].items():
            if cik not in f["ni"] or cik not in sic_map:
                continue
            sector = us.sic_to_sector(sic_map[cik][1], cik)
            if not sector or r <= 0:
                continue
            byc[cik] = {"sector": sector, "rev": r, "ni": f["ni"][cik],
                        "op": f["op"].get(cik), "eq": f["eq"].get(cik)}
        per_q[(y, q)] = byc

    rows = []
    for i in range(3, len(quarters)):
        window = quarters[i - 3:i + 1]
        y, q = quarters[i]
        # 四季都有申報的公司才納入，確保 TTM 是同一組公司
        common = set(per_q[window[0]])
        for k in window[1:]:
            common &= set(per_q[k])
        if not common:
            continue

        agg: dict[str, dict] = defaultdict(
            lambda: {"rev": 0.0, "op": 0.0, "ni": 0.0, "eq": 0.0,
                     "n": 0, "n_op": 0, "n_eq": 0})
        for cik in common:
            last = per_q[(y, q)][cik]
            a = agg[last["sector"]]
            a["rev"] += sum(per_q[k][cik]["rev"] for k in window)
            a["ni"] += sum(per_q[k][cik]["ni"] for k in window)
            a["n"] += 1
            ops = [per_q[k][cik]["op"] for k in window]
            if all(v is not None for v in ops):
                a["op"] += sum(ops)
                a["n_op"] += 1
            if last["eq"]:
                a["eq"] += last["eq"]
                a["n_eq"] += 1

        for sec, a in sorted(agg.items()):
            if a["n"] < 8 or not a["rev"]:
                continue
            # 整個類股的淨利率不可能超出這個範圍。真的算出來就是上游資料
            # 或回推邏輯有問題，寧可不輸出也不要放一個錯的數字進去。
            nm = 100 * a["ni"] / a["rev"]
            if not (-100 <= nm <= 100):
                print(f"  [略過] {y}Q{q} {sec}：淨利率 {nm:.1f}% 不合理")
                continue
            # 營業利益只有部分公司申報，覆蓋不足就不輸出，免得比率失真
            op_ok = a["n_op"] >= max(8, a["n"] * 0.5)
            rows.append({
                "quarter": f"{y}Q{q}",
                "industry": sec,
                "op_margin": round(100 * a["op"] / a["rev"], 4) if op_ok else "",
                "net_margin": round(100 * a["ni"] / a["rev"], 4),
                "roe": (round(100 * a["ni"] / a["eq"], 4)
                        if a["n_eq"] >= max(8, a["n"] * 0.5) and a["eq"] else ""),
                "n": a["n"],
            })

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "quarter", "industry", "op_margin", "net_margin", "roe", "n"])
        w.writeheader()
        w.writerows(rows)

    qs = sorted({r["quarter"] for r in rows})
    inds = sorted({r["industry"] for r in rows})
    print()
    print(f"輸出 {OUT}")
    print(f"  {len(rows):,} 列　{len(qs)} 個 TTM 期間（{qs[0]} ~ {qs[-1]}）"
          f"　{len(inds)} 個類股")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=32)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    main(a.quarters, a.refresh)
