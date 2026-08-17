"""資料驗證：每次更新後自動檢查看板上的數字對不對。

為什麼需要這個
--------------
這個看板是給家人朋友看的，但沒有人有辦法用肉眼確認 32 個類股、上千檔個股的
數字是否正確。所以正確性必須靠機器定期驗證，而且驗證方式要能抓到「解析錯誤」
與「來源變更」這兩種最危險的問題——它們不會讓程式當掉，只會讓數字悄悄變錯。

五類檢查
--------
1. 跨來源對帳  同一個事實用兩個獨立系統各抓一次，比對是否一致。
                月營收：證交所 OpenAPI vs 公開資訊觀測站
                季報　：證交所 OpenAPI vs 公開資訊觀測站
                這是最有價值的一類——它抓得到解析錯位、單位錯誤、欄位改名。
2. 不變量      數學上一定要成立的事。排名必須是 1..N 不重複、百分位必須在
                0~100、家數必須大於 0、比率不能落在物理上不可能的範圍。
3. 時效性      每個資料源的日期是否符合它自己的公告時程。抓得到「上游停更但
                我們照樣輸出舊資料」這種無聲失敗。
4. 連續性      跟上一次的結果比。分數一夜之間暴衝通常代表資料出問題，而不是
                產業真的翻天覆地。
5. 覆蓋率      有多少比重的指標真的有資料、多少個股歸不了類。

輸出 web/data/verify.json，前端會顯示狀態；CI 遇到 fail 會讓建置失敗。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tpex  # noqa: E402
import twse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
WEB = ROOT / "web" / "data"
PREV = HIST / "score_snapshot.json"
OUT = WEB / "verify.json"

# 對帳容許誤差。兩邊都是同一批申報資料，理論上要完全一致；留 0.5% 是因為
# 兩個系統的更新時點可能差幾分鐘，剛好卡在公司改申報的當下。
TOL = 0.005
# 不一致家數超過這個比例就算失敗，而不是零容忍——總有一兩家在改申報。
FAIL_RATIO = 0.02


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, cid, group, label, status, detail, metric=None):
        self.checks.append({"id": cid, "group": group, "label": label,
                            "status": status, "detail": detail,
                            "metric": metric or {}})

    @property
    def status(self) -> str:
        s = {c["status"] for c in self.checks}
        return "fail" if "fail" in s else ("warn" if "warn" in s else "pass")


def load(name: str):
    return json.loads((SNAP / f"{name}.json").read_text(encoding="utf-8"))


def num(x):
    return twse.num(x)


# ------------------------------------------------------------- 1. 跨來源對帳
def check_revenue_reconcile(rep: Report) -> None:
    """月營收：證交所 OpenAPI 對上公開資訊觀測站的 HTML 表格。"""
    try:
        api = {str(r["公司代號"]).strip(): num(r.get("營業收入-當月營收"))
               for r in load("monthly_revenue")}
        period = tpex.latest_revenue_period("sii")
        if period is None:
            raise RuntimeError("找不到公開資訊觀測站的上市月營收")
        mops = {r["公司代號"]: r["營業收入-當月營收"]
                for r in tpex.fetch_monthly_revenue(*period, market="sii")}
    except Exception as exc:  # noqa: BLE001
        rep.add("recon_rev", "跨來源對帳", "月營收（證交所 vs 公開資訊觀測站）",
                "warn", f"抓不到對照來源：{type(exc).__name__} {str(exc)[:70]}")
        return

    both = set(api) & set(mops)
    if len(both) < 500:
        rep.add("recon_rev", "跨來源對帳", "月營收（證交所 vs 公開資訊觀測站）",
                "warn", f"只有 {len(both)} 家能對照，樣本太少無法判斷")
        return

    bad = []
    for code in both:
        a, m = api[code], mops[code]
        if a is None or m is None:
            continue
        if a == 0 and m == 0:
            continue
        base = max(abs(a), abs(m), 1)
        if abs(a - m) / base > TOL:
            bad.append((code, a, m))

    ratio = len(bad) / len(both)
    status = "pass" if ratio == 0 else ("warn" if ratio <= FAIL_RATIO else "fail")
    detail = (f"{len(both):,} 家可對照，{len(bad)} 家不一致"
              f"（{ratio*100:.2f}%）")
    if bad[:3]:
        detail += "；例：" + "、".join(
            f"{c} {a:,.0f} vs {m:,.0f}" for c, a, m in bad[:3])
    rep.add("recon_rev", "跨來源對帳", "月營收（證交所 vs 公開資訊觀測站）",
            status, detail, {"compared": len(both), "mismatch": len(bad)})


def check_quarterly_reconcile(rep: Report) -> None:
    """季報營業收入：證交所 OpenAPI 對上公開資訊觀測站 t163sb04。"""
    try:
        import hist_fin
        api = {}
        for r in load("income_general"):
            api[str(r["公司代號"]).strip()] = num(r.get("營業收入"))
        y, q = hist_fin.quarters_back(1)[0]
        mops = hist_fin._parse(
            hist_fin._fetch("t163sb04", y, q), {"rev": "營業收入"})
        mops = {k: v["rev"] for k, v in mops.items()}
    except Exception as exc:  # noqa: BLE001
        rep.add("recon_q", "跨來源對帳", "季報營收（證交所 vs 公開資訊觀測站）",
                "warn", f"抓不到對照來源：{type(exc).__name__} {str(exc)[:70]}")
        return

    both = set(api) & set(mops)
    if len(both) < 300:
        rep.add("recon_q", "跨來源對帳", "季報營收（證交所 vs 公開資訊觀測站）",
                "warn", f"只有 {len(both)} 家能對照，樣本太少無法判斷")
        return

    bad = []
    for code in both:
        a, m = api[code], mops[code]
        if a is None or m is None:
            continue
        base = max(abs(a), abs(m), 1)
        if abs(a - m) / base > TOL:
            bad.append((code, a, m))

    ratio = len(bad) / len(both)
    status = "pass" if ratio == 0 else ("warn" if ratio <= FAIL_RATIO else "fail")
    detail = f"{len(both):,} 家可對照，{len(bad)} 家不一致（{ratio*100:.2f}%）"
    if bad[:3]:
        detail += "；例：" + "、".join(
            f"{c} {a:,.0f} vs {m:,.0f}" for c, a, m in bad[:3])
    rep.add("recon_q", "跨來源對帳", "季報營收（證交所 vs 公開資訊觀測站）",
            status, detail, {"compared": len(both), "mismatch": len(bad)})


# ------------------------------------------------------------- 2. 不變量
def check_invariants(rep: Report, dash: dict, label: str) -> None:
    secs = dash["sectors"]
    problems: list[str] = []

    for hz in dash["horizons"]:
        scores = [s["scores"][hz]["score"] for s in secs]
        ranks = [s["scores"][hz]["rank"] for s in secs]
        if any(not (0 <= v <= 100) for v in scores):
            problems.append(f"{hz}：分數超出 0~100")
        if sorted(ranks) != list(range(1, len(secs) + 1)):
            problems.append(f"{hz}：排名不是 1~{len(secs)} 的不重複序列")
        cov = [s["scores"][hz]["coverage"] for s in secs]
        if any(not (0 <= v <= 100) for v in cov):
            problems.append(f"{hz}：覆蓋率超出 0~100")

    for s in secs:
        r = s["raw"]
        if s.get("members", 0) <= 0:
            problems.append(f"{s['industry']}：家數為 {s.get('members')}")
        for k, lo, hi in (("op_margin", -300, 100), ("net_margin", -300, 100),
                          ("roe", -300, 300), ("pe", 0, 1000), ("pb", 0, 100)):
            v = r.get(k)
            if v is not None and not (lo <= v <= hi):
                problems.append(f"{s['industry']} {k}={v} 落在不可能的範圍")
        for k in list(r):
            if k.endswith("_pct") and r[k] is not None and not (0 <= r[k] <= 100):
                problems.append(f"{s['industry']} {k}={r[k]} 不是百分位")
        rrg = s.get("rrg")
        if rrg and rrg.get("path"):
            if len(rrg["path"]) % 2:
                problems.append(f"{s['industry']}：軌跡座標數為奇數")
            if rrg.get("path_dates") and len(rrg["path"]) != len(rrg["path_dates"]) * 2:
                problems.append(f"{s['industry']}：軌跡座標與日期數量對不上")

    status = "pass" if not problems else "fail"
    detail = ("全部通過" if not problems
              else f"{len(problems)} 項異常：" + "；".join(problems[:4]))
    rep.add(f"invariant_{label}", "不變量", f"數學一致性（{label}）",
            status, detail, {"violations": len(problems)})


# ------------------------------------------------------------- 3. 時效性
def check_freshness(rep: Report, dash: dict, label: str) -> None:
    asof = dash.get("data_asof", {})
    today = dt.date.today()
    items: list[tuple[str, str, int]] = []   # (欄位, 值, 落後天數上限)

    price = asof.get("price")
    if price:
        try:
            gap = (today - dt.date.fromisoformat(price)).days
            # 連假最多 5 天，再加一天緩衝
            items.append(("股價", price, gap))
        except ValueError:
            pass

    problems, notes = [], []
    for name, value, gap in items:
        notes.append(f"{name} {value}（{gap} 天前）")
        if gap > 6:
            problems.append(f"{name}已 {gap} 天沒更新")

    rev = asof.get("revenue")
    if rev:
        notes.append(f"月營收 {rev}")
        m = re.search(r"(\d{3})年(\d{1,2})月", str(rev))
        if m:
            ry, rm = int(m.group(1)) + 1911, int(m.group(2))
            # 每月 10 日前公告上個月，所以 15 日之後還停在兩個月前就不對
            months_behind = (today.year - ry) * 12 + today.month - rm
            if months_behind > (2 if today.day < 12 else 1):
                problems.append(f"月營收落後 {months_behind} 個月")

    status = "pass" if not problems else "warn"
    rep.add(f"fresh_{label}", "時效性", f"資料新鮮度（{label}）", status,
            ("；".join(notes) if not problems
             else "；".join(problems) + "　|　" + "；".join(notes)))


# ------------------------------------------------------------- 4. 連續性
def check_continuity(rep: Report, dashes: dict[str, dict]) -> None:
    prev = {}
    if PREV.exists():
        try:
            prev = json.loads(PREV.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}

    now = {mk: {s["industry"]: {hz: s["scores"][hz]["score"]
                                for hz in d["horizons"]}
                for s in d["sectors"]}
           for mk, d in dashes.items()}

    jumps = []
    for mk, cur in now.items():
        old = prev.get(mk, {})
        for ind, byhz in cur.items():
            for hz, v in byhz.items():
                o = old.get(ind, {}).get(hz)
                if o is None:
                    continue
                if abs(v - o) >= 40:
                    jumps.append(f"{mk} {ind} {hz} {o:.0f}→{v:.0f}")

    if not prev:
        status, detail = "pass", "第一次執行，還沒有可比較的前次結果"
    elif jumps:
        status = "warn"
        detail = f"{len(jumps)} 項分數大幅跳動：" + "；".join(jumps[:4])
    else:
        status, detail = "pass", "沒有異常跳動（門檻 40 分）"

    rep.add("continuity", "連續性", "與前次結果比較", status, detail,
            {"jumps": len(jumps)})
    PREV.parent.mkdir(parents=True, exist_ok=True)
    PREV.write_text(json.dumps(now, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------- 5. 覆蓋率
def check_coverage(rep: Report, dash: dict, label: str) -> None:
    secs = dash["sectors"]
    worst = []
    for hz in dash["horizons"]:
        for s in secs:
            c = s["scores"][hz]["coverage"]
            if c < 60:
                worst.append(f"{s['industry']} {hz} {c:.0f}%")
    avg = sum(s["scores"]["medium"]["coverage"] for s in secs) / len(secs)

    status = "pass" if not worst else "warn"
    detail = f"中期平均覆蓋率 {avg:.0f}%"
    if worst:
        detail += f"；{len(worst)} 項低於 60%：" + "；".join(worst[:4])
    rep.add(f"coverage_{label}", "覆蓋率", f"指標完整度（{label}）", status,
            detail, {"avg": round(avg, 1), "low": len(worst)})


def check_classification(rep: Report) -> None:
    try:
        idx = json.loads((WEB / "stocks_tw.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        rep.add("classify", "覆蓋率", "個股產業歸類", "warn",
                f"讀不到對照表：{str(exc)[:60]}")
        return
    inds = idx["industries"]
    other = inds.index("其他") if "其他" in inds else -1
    n = len(idx["stocks"])
    n_other = sum(1 for s in idx["stocks"] if s[2] == other)
    ratio = n_other / n if n else 0
    status = "pass" if ratio < 0.10 else ("warn" if ratio < 0.20 else "fail")
    rep.add("classify", "覆蓋率", "個股產業歸類", status,
            f"{n:,} 檔中有 {n_other} 檔歸在「其他」（{ratio*100:.1f}%）",
            {"total": n, "other": n_other})


# ------------------------------------------------------------- 主流程
def main(skip_network: bool = False) -> int:
    rep = Report()
    dashes = {}
    for mk, fn in (("台股", "dashboard.json"), ("美股", "dashboard_us.json")):
        p = WEB / fn
        if p.exists():
            dashes[mk] = json.loads(p.read_text(encoding="utf-8"))

    if not dashes:
        print("找不到任何 dashboard，請先執行 build.py")
        return 2

    if not skip_network:
        check_revenue_reconcile(rep)
        check_quarterly_reconcile(rep)
    for mk, d in dashes.items():
        check_invariants(rep, d, mk)
        check_freshness(rep, d, mk)
        check_coverage(rep, d, mk)
    check_classification(rep)
    check_continuity(rep, dashes)

    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": rep.status,
        "summary": {
            "pass": sum(1 for c in rep.checks if c["status"] == "pass"),
            "warn": sum(1 for c in rep.checks if c["status"] == "warn"),
            "fail": sum(1 for c in rep.checks if c["status"] == "fail"),
        },
        "checks": rep.checks,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    icon = {"pass": "[通過]", "warn": "[注意]", "fail": "[失敗]"}
    print("=" * 70)
    print("資料驗證")
    print("=" * 70)
    group = None
    for c in rep.checks:
        if c["group"] != group:
            group = c["group"]
            print(f"\n{group}")
        print(f"  {icon[c['status']]} {c['label']}")
        print(f"         {c['detail']}")
    s = payload["summary"]
    print()
    print("=" * 70)
    print(f"總結：通過 {s['pass']}　注意 {s['warn']}　失敗 {s['fail']}"
          f"　-> {rep.status.upper()}")
    print(f"輸出 {OUT}")
    return 1 if rep.status == "fail" else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-network", action="store_true",
                    help="只做本地檢查，不做跨來源對帳")
    a = ap.parse_args()
    sys.exit(main(a.skip_network))
