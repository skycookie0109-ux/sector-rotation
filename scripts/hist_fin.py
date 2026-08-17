"""回補各產業的歷史季報，用來把「水準型」指標改成相對自己歷史。

為什麼要做這件事
----------------
原本營益率、ROE、本益比都是拿 32 個類股互相比排名。但半導體營益率 44%、
電子通路 2.65%，這不是體質差距，是商業模式差異——通路業本來就是低毛利
高周轉。結果高毛利產業在中長期榜上有結構性優勢，跟「這個產業正在變好」
完全無關。

改成拿每個產業跟自己的歷史比：營益率落在自身過去幾年區間的第幾百分位。
這樣通路業從 2.4% 進步到 2.65% 會被認可，半導體從 46% 掉到 44% 也會被扣分。

兩個技術重點
------------
1. 公開資訊觀測站的季報是「累計至第 N 季」而不是單季，所以要先還原成單季
   （Q_n = 累計_n - 累計_{n-1}），否則 Q1 跟 Q4 根本不能比。
2. 單季數字有季節性（零售旺在 Q4、面板有淡旺季），所以再滾成 TTM（近四季
   合計）才拿來比。TTM 是消除季節性的標準做法。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
HIST.mkdir(parents=True, exist_ok=True)
OUT = HIST / "fin_history.csv"

MOPS = "https://mopsov.twse.com.tw/mops/web/ajax_"
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
})

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(x: str) -> str:
    return _TAG_RE.sub("", x).replace("&nbsp;", " ").replace("　", " ").strip()


def _fetch(form: str, year: int, season: int, *, cache: bool = True) -> str:
    """抓一季的報表。整頁快取起來，回補跑第二次就不必再下載 1.5MB。"""
    cp = HIST / "cache" / f"{form}_{year}_{season}.html"
    cp.parent.mkdir(parents=True, exist_ok=True)
    if cache and cp.exists() and cp.stat().st_size > 50_000:
        return cp.read_text(encoding="utf-8")

    for attempt in range(3):
        try:
            r = _SESSION.post(f"{MOPS}{form}", timeout=90, data={
                "encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
                "TYPEK": "sii", "year": str(year), "season": f"{season:02d}",
            })
            r.raise_for_status()
            r.encoding = "utf-8"
            if len(r.content) < 50_000:
                raise RuntimeError(f"回應太小（{len(r.content)} bytes），可能是該季無資料")
            cp.write_text(r.text, encoding="utf-8")
            return r.text
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                raise RuntimeError(f"{form} {year}Q{season}: {exc}") from exc
            time.sleep(4 * (attempt + 1))
    raise RuntimeError("unreachable")


def _parse(html: str, wanted: dict[str, str]) -> dict[str, dict[str, float]]:
    """挑出一般業那張表，依欄位「名稱」取值。

    一次回應裡有金融業、一般業、證券業等好幾張表，欄位完全不同；而且欄位
    順序在不同季度之間會變動。所以先找出含有目標欄位的表，再用名稱定位，
    不用固定的索引。
    """
    best: dict[str, dict[str, float]] = {}
    for tb in _TABLE_RE.findall(html):
        head = [_clean(x) for x in _CELL_RE.findall(
            _ROW_RE.findall(tb)[0] if _ROW_RE.findall(tb) else "")]
        pos = {}
        for key, label in wanted.items():
            if label in head:
                pos[key] = head.index(label)
        if len(pos) < len(wanted):
            continue

        rows: dict[str, dict[str, float]] = {}
        for chunk in _ROW_RE.findall(tb):
            cells = [_clean(c) for c in _CELL_RE.findall(chunk)]
            if not cells or not re.fullmatch(r"\d{4}", cells[0] or ""):
                continue
            rec = {}
            for key, i in pos.items():
                rec[key] = twse.num(cells[i]) if i < len(cells) else None
            rows[cells[0]] = rec
        if len(rows) > len(best):
            best = rows
    return best


def industry_map() -> dict[str, str]:
    """代號 -> 產業別。用目前的月營收快照，只涵蓋上市（季報本來就只有上市）。"""
    path = SNAP / "monthly_revenue.json"
    if not path.exists():
        raise SystemExit("找不到 monthly_revenue.json，請先執行 fetch.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(r["公司代號"]).strip(): r.get("產業別", "其他").strip() for r in data}


def quarters_back(n: int) -> list[tuple[int, int]]:
    """從最近一季往回列 n 季，回傳 (民國年, 季)。"""
    import datetime as _dt
    today = _dt.date.today()
    roc = today.year - 1911
    # 季報公告時點：Q1 5/15、Q2 8/14、Q3 11/14、年報 3/31
    if today.month >= 11:
        y, q = roc, 3
    elif today.month >= 8:
        y, q = roc, 2
    elif today.month >= 5:
        y, q = roc, 1
    elif today.month >= 4:
        y, q = roc - 1, 4
    else:
        y, q = roc - 1, 3

    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def main(n_quarters: int = 18, refresh: bool = False) -> None:
    imap = industry_map()
    quarters = list(reversed(quarters_back(n_quarters)))   # 由舊到新
    latest = quarters[-1]

    # 一季才多一筆，但重建要下載 8 年份、約 100 MB。所以先看現有的 CSV
    # 是否已經涵蓋最新一季——涵蓋就直接跳過。這讓每天跑的排程幾乎零流量，
    # 只有每季財報公布後那次才會真的重建。
    if OUT.exists() and not refresh:
        with OUT.open(encoding="utf-8") as fh:
            have = {r["quarter"] for r in csv.DictReader(fh)}
        if f"{latest[0]}Q{latest[1]}" in have:
            print(f"歷史季報已是最新（{latest[0]}Q{latest[1]}），跳過")
            return
    print("=" * 68)
    print(f"回補歷史季報：{quarters[0][0]}Q{quarters[0][1]} ~ "
          f"{quarters[-1][0]}Q{quarters[-1][1]}（{len(quarters)} 季）")
    print("=" * 68)

    # cum[code][(y,q)] = {rev, op, ni, eq}
    cum: dict[str, dict[tuple[int, int], dict]] = {}
    for y, q in quarters:
        try:
            inc = _parse(_fetch("t163sb04", y, q, cache=not refresh), {
                "rev": "營業收入",
                "op": "營業利益（損失）",
                "ni": "淨利（淨損）歸屬於母公司業主",
            })
            bal = _parse(_fetch("t163sb05", y, q, cache=not refresh), {
                "eq": "歸屬於母公司業主之權益合計",
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳過] {y}Q{q}  {str(exc)[:60]}")
            continue

        for code, rec in inc.items():
            rec["eq"] = bal.get(code, {}).get("eq")
            cum.setdefault(code, {})[(y, q)] = rec
        print(f"  [OK]   {y}Q{q}  損益 {len(inc):,} 家  資產負債 {len(bal):,} 家")
        time.sleep(1.0)

    if not cum:
        raise SystemExit("一季都沒抓到，中止")

    # 累計 -> 單季。Q1 本身就是單季；Q2~Q4 要減掉前一季的累計。
    single: dict[str, dict[tuple[int, int], dict]] = {}
    for code, byq in cum.items():
        for (y, q), rec in byq.items():
            if q == 1:
                rev, op, ni = rec.get("rev"), rec.get("op"), rec.get("ni")
            else:
                prev = byq.get((y, q - 1))
                if not prev:
                    continue
                def sub(a, b):
                    return None if a is None or b is None else a - b
                rev = sub(rec.get("rev"), prev.get("rev"))
                op = sub(rec.get("op"), prev.get("op"))
                ni = sub(rec.get("ni"), prev.get("ni"))
            single.setdefault(code, {})[(y, q)] = {
                "rev": rev, "op": op, "ni": ni, "eq": rec.get("eq")}

    # 單季 -> TTM（近四季合計），再依產業彙總。權益取期末值。
    order = quarters
    rows = []
    for i in range(3, len(order)):
        window = order[i - 3:i + 1]
        y, q = order[i]
        agg: dict[str, dict[str, float]] = {}
        for code, byq in single.items():
            ind = imap.get(code)
            if not ind:
                continue
            vals = [byq.get(k) for k in window]
            if any(v is None for v in vals):
                continue
            if any(v["rev"] is None or v["op"] is None or v["ni"] is None for v in vals):
                continue
            eq = byq[(y, q)].get("eq")
            a = agg.setdefault(ind, {"rev": 0.0, "op": 0.0, "ni": 0.0,
                                     "eq": 0.0, "n": 0, "n_eq": 0})
            a["rev"] += sum(v["rev"] for v in vals)
            a["op"] += sum(v["op"] for v in vals)
            a["ni"] += sum(v["ni"] for v in vals)
            a["n"] += 1
            if eq:
                a["eq"] += eq
                a["n_eq"] += 1

        for ind, a in sorted(agg.items()):
            if a["n"] < 3 or not a["rev"]:
                continue
            rows.append({
                "quarter": f"{y}Q{q}",
                "industry": ind,
                "op_margin": round(100 * a["op"] / a["rev"], 4),
                "net_margin": round(100 * a["ni"] / a["rev"], 4),
                # TTM 淨利對期末權益，不需要再年化
                "roe": (round(100 * a["ni"] / a["eq"], 4)
                        if a["n_eq"] >= 3 and a["eq"] else ""),
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
          f"　{len(inds)} 個產業")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=18)
    ap.add_argument("--refresh", action="store_true", help="忽略快取重新下載")
    a = ap.parse_args()
    main(a.quarters, a.refresh)
