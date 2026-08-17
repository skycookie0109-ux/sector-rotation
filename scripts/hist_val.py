"""回補各產業的歷史估值（本益比、股價淨值比），用來把估值也改成相對自己歷史。

跟營益率同樣的道理：金融股本益比天生就低、成長股天生就高，拿 32 個類股的
本益比互相排名，等於在獎勵產業屬性而不是「現在便宜不便宜」。改成看目前的
本益比落在自己過去幾年區間的第幾百分位，才是真的在講貴或便宜。

證交所的 BWIBBU_d 可以指定任意日期，所以每個月取一天回補即可。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
HIST.mkdir(parents=True, exist_ok=True)
OUT = HIST / "val_history.csv"

URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
       "?date={d}&selectType=ALL&response=json")


def industry_map() -> dict[str, str]:
    path = SNAP / "monthly_revenue.json"
    if not path.exists():
        raise SystemExit("找不到 monthly_revenue.json，請先執行 fetch.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(r["公司代號"]).strip(): r.get("產業別", "其他").strip() for r in data}


def month_slots(months: int) -> list[list[dt.date]]:
    """每個月給一組候選日期。

    以 15 號為主，但那天可能是週末或連假，證交所會回空資料。所以往後再備
    幾天，第一個抓得到的就用，一個月只留一筆。
    """
    out = []
    today = dt.date.today()
    y, m = today.year, today.month
    for _ in range(months):
        cands = []
        for off in (0, 1, 2, -1, 3, -2, 4):
            try:
                d = dt.date(y, m, 15 + off)
            except ValueError:
                continue
            if d < today:
                cands.append(d)
        if cands:
            out.append(cands)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out, key=lambda c: c[0])


def main(months: int = 48, refresh: bool = False) -> None:
    imap = industry_map()
    slots = month_slots(months)
    existing: dict[str, dict] = {}
    if OUT.exists() and not refresh:
        for r in csv.DictReader(OUT.open(encoding="utf-8")):
            existing[f"{r['date']}|{r['industry']}"] = r
        done = {r["date"][:7] for r in existing.values()}   # 以「年-月」判斷是否已有
    else:
        done = set()

    print("=" * 68)
    print(f"回補歷史估值：{slots[0][0]} ~ {slots[-1][0]}（{len(slots)} 個月）")
    print("=" * 68)

    rows: list[dict] = list(existing.values())
    added = 0
    for cands in slots:
        if cands[0].strftime("%Y-%m") in done:
            continue

        recs, key = [], None
        for d in cands:
            try:
                data = twse.fetch(URL.format(d=d.strftime("%Y%m%d")), cache=True)
            except Exception:  # noqa: BLE001
                time.sleep(1.2)
                continue
            if data.get("data"):
                recs, key = data["data"], d.isoformat()
                break
            time.sleep(1.2)

        if not recs:
            print(f"  [空]   {cands[0].strftime('%Y-%m')}  這個月的候選日期都沒有資料")
            continue

        by_ind: dict[str, dict[str, list[float]]] = {}
        for row in recs:
            code = str(row[0]).strip()
            ind = imap.get(code)
            if not ind:
                continue
            slot = by_ind.setdefault(ind, {"pe": [], "pb": [], "dy": []})
            pe, pb, dy = twse.num(row[5]), twse.num(row[6]), twse.num(row[3])
            # 本益比為 0 或空白代表虧損，不能當成「超便宜」拉低中位數
            if pe and pe > 0:
                slot["pe"].append(pe)
            if pb and pb > 0:
                slot["pb"].append(pb)
            if dy is not None and dy > 0:
                slot["dy"].append(dy)

        n_ind = 0
        for ind, slot in by_ind.items():
            if len(slot["pe"]) < 3:
                continue
            rows.append({
                "date": key, "industry": ind,
                "pe": round(statistics.median(slot["pe"]), 3),
                "pb": (round(statistics.median(slot["pb"]), 3)
                       if len(slot["pb"]) >= 3 else ""),
                "div_yield": (round(statistics.median(slot["dy"]), 3)
                              if len(slot["dy"]) >= 3 else ""),
                "n": len(slot["pe"]),
            })
            n_ind += 1
        added += 1
        print(f"  [OK]   {key}  {len(recs):,} 檔 -> {n_ind} 個產業")
        time.sleep(1.2)

    rows.sort(key=lambda r: (r["date"], r["industry"]))
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "industry", "pe", "pb",
                                           "div_yield", "n"])
        w.writeheader()
        w.writerows(rows)

    ds = sorted({r["date"] for r in rows})
    print()
    print(f"輸出 {OUT}")
    print(f"  {len(rows):,} 列　{len(ds)} 個月（{ds[0]} ~ {ds[-1]}）"
          f"　新增 {added} 個月")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=48)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    main(a.months, a.refresh)
