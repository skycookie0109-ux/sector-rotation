"""抓取三大法人買賣超，彙總成產業層級的籌碼面資料。

正規化方式：淨買超股數 ÷ 該產業總發行股數。
意思是「這段期間法人買走了該產業幾 % 的股本」，跨產業可直接比較，
而且不需要估市值（避開股價與股本單位換算的誤差）。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
HIST.mkdir(parents=True, exist_ok=True)
CSV_PATH = HIST / "chips.csv"

FIELD_ALIASES = {
    "foreign": ["外陸資買賣超股數(不含外資自營商)", "外資買賣超股數"],
    "trust": ["投信買賣超股數"],
    "dealer": ["自營商買賣超股數"],
}


def _trading_days(limit: int) -> list[str]:
    """從已回補的指數歷史取最近的交易日，避免自己猜哪天有開市。"""
    path = HIST / "index_history.csv"
    if not path.exists():
        raise SystemExit("請先執行 backfill.py 取得交易日曆")
    with path.open(encoding="utf-8", newline="") as fh:
        days = sorted({row["date"] for row in csv.DictReader(fh)})
    return days[-limit:]


def _stock_industry() -> dict[str, str]:
    rev = json.loads((SNAP / "monthly_revenue.json").read_text(encoding="utf-8"))
    return {r["公司代號"]: r["產業別"] for r in rev}


def _pick(row: dict, keys: list[str]):
    for k in keys:
        if k in row:
            return twse.num(row[k], 0.0)
    return 0.0


def main(days: int = 20) -> None:
    industry_of = _stock_industry()
    targets = _trading_days(days)
    done = set()
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8", newline="") as fh:
            done = {r["date"] for r in csv.DictReader(fh)}
    targets = [d for d in targets if d not in done]

    if not targets:
        print(f"籌碼面已是最新（{len(done)} 個交易日）")
        return

    print(f"抓取三大法人買賣超：{len(targets)} 個交易日")
    rows_out = []
    for i, day in enumerate(targets, 1):
        stamp = day.replace("-", "")
        try:
            payload = twse.t86(stamp)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(targets)}] {day} 失敗 {type(exc).__name__}")
            continue
        if payload.get("stat") != "OK":
            continue

        fields = payload.get("fields", [])
        agg: dict[str, dict[str, float]] = {}
        for raw in payload.get("data", []):
            row = dict(zip(fields, raw))
            code = str(row.get("證券代號", "")).strip()
            ind = industry_of.get(code)
            if ind is None:          # ETF、權證、非上市普通股
                continue
            bucket = agg.setdefault(ind, {"foreign": 0.0, "trust": 0.0, "dealer": 0.0})
            for key, aliases in FIELD_ALIASES.items():
                bucket[key] += _pick(row, aliases)

        for ind, vals in agg.items():
            rows_out.append([day, ind, vals["foreign"], vals["trust"], vals["dealer"]])
        print(f"  [{i}/{len(targets)}] {day}  {len(agg)} 個產業")

    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(["date", "industry", "foreign_net", "trust_net", "dealer_net"])
        w.writerows(rows_out)
    print(f"\n完成：新增 {len(rows_out):,} 列 -> {CSV_PATH}")


if __name__ == "__main__":
    n = 20
    if "--days" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1])
    main(n)
