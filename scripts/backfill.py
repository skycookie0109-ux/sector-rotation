"""回補各類股指數的歷史收盤，技術面 / RRG 需要。

證交所沒有提供批次下載，只能一天一次請求（每次約 29KB）。
首次執行約 12 分鐘；之後有快取，只會補新的交易日。

用法：
    python scripts/backfill.py            # 預設回補 500 個日曆日
    python scripts/backfill.py --days 900 # 回補更久（長期分析建議 2 年以上）
"""
from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402
from sectors import BENCHMARK_INDEX, normalize_index_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "history"
HIST.mkdir(parents=True, exist_ok=True)
CSV_PATH = HIST / "index_history.csv"

PRICE_TABLE_KEY = "價格指數(臺灣證券交易所)"


def _existing_dates() -> set[str]:
    if not CSV_PATH.exists():
        return set()
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        return {row["date"] for row in csv.DictReader(fh)}


def _extract(payload: dict) -> list[tuple[str, float]]:
    """從 MI_INDEX 回應取出『價格指數(臺灣證券交易所)』那張表。

    只要類指數與大盤，其他（跨市場、台灣指數公司、報酬指數）忽略。
    """
    for table in payload.get("tables", []):
        title = str(table.get("title", ""))
        if PRICE_TABLE_KEY not in title:
            continue
        out = []
        for row in table.get("data", []):
            if len(row) < 2:
                continue
            name = normalize_index_name(row[0])
            close = twse.num(row[1])
            if close is None:
                continue
            if name.endswith("類指數") or name == BENCHMARK_INDEX:
                out.append((name, close))
        return out
    return []


def main(days: int = 500) -> None:
    done = _existing_dates()
    today = date.today()
    targets = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        if d.weekday() >= 5:          # 六日不開市
            continue
        if d.isoformat() in done:
            continue
        targets.append(d)

    if not targets:
        print(f"已是最新，共 {len(done)} 個交易日在 {CSV_PATH.name}")
        return

    print(f"待回補 {len(targets)} 個日期（已有 {len(done)} 個交易日）")
    print(f"預估耗時約 {len(targets) * 2 / 60:.0f} 分鐘，可中斷後重跑續補\n")

    new_rows, holidays, failures = [], 0, 0
    for i, d in enumerate(targets, 1):
        stamp = d.strftime("%Y%m%d")
        try:
            payload = twse.mi_index(stamp)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  [{i}/{len(targets)}] {d} 失敗 {type(exc).__name__}")
            continue

        rows = _extract(payload) if payload.get("stat") == "OK" else []
        if not rows:
            holidays += 1
        else:
            new_rows.extend((d.isoformat(), name, close) for name, close in rows)

        if i % 20 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] {d}  累計 {len(new_rows):,} 列"
                  f"（非交易日 {holidays}、失敗 {failures}）")

    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(["date", "index_name", "close"])
        w.writerows(new_rows)

    total = len(_existing_dates())
    print(f"\n完成：新增 {len(new_rows):,} 列，累計 {total} 個交易日 -> {CSV_PATH}")
    if failures:
        print(f"注意：有 {failures} 天抓取失敗，再跑一次即可補上")


if __name__ == "__main__":
    n = 500
    if "--days" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1])
    main(n)
