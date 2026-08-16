"""把原始資料算成三種期間的類股輪動分數，輸出 web/data/dashboard.json。

設計原則
--------
1. 基本面主導中長期。季報一季才更新一次，所以「用基本面預測一個月內的漲跌」
   在資訊上是不成立的——那段期間根本沒有新的基本面訊息進來。短期分頁因此
   明確以籌碼與價格動能為主，並在畫面上標示清楚。
2. 所有指標都先做跨產業的穩健標準化（median / MAD），再加權合成，
   避免單一離群產業（例如航運業獲利暴衝那種）整組洗掉。
3. 缺資料就把該指標的權重重新分配給其他指標，並記錄 coverage 讓前端顯示。
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402
from sectors import (BENCHMARK_INDEX, EXCLUDED_INDUSTRY, WEAK_QUARTERLY,  # noqa: E402
                     normalize_index_name, resolve)

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
OUT = ROOT / "web" / "data"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 指標定義
# (欄位, 顯示名, 方向)  方向 -1 代表數字越小越好
METRICS = {
    # 基本面 —— 月營收，每月更新，全產業都有
    "rev_yoy":       ("月營收年增率", 1, "fundamental"),
    "rev_accel":     ("營收動能加速度", 1, "fundamental"),
    "rev_breadth":   ("營收成長家數占比", 1, "fundamental"),
    "rev_yoy_cum":   ("累計營收年增率", 1, "fundamental"),
    # 基本面 —— 季報，一季更新一次
    "op_margin":     ("營業利益率", 1, "fundamental"),
    "net_margin":    ("稅後淨利率", 1, "fundamental"),
    "roe":           ("股東權益報酬率(年化)", 1, "fundamental"),
    "profit_breadth": ("獲利家數占比", 1, "fundamental"),
    # 估值
    "pe":            ("本益比", -1, "valuation"),
    "pb":            ("股價淨值比", -1, "valuation"),
    "div_yield":     ("現金殖利率", 1, "valuation"),
    # 籌碼
    "foreign_flow":  ("外資買超佔股本比", 1, "chips"),
    "trust_flow":    ("投信買超佔股本比", 1, "chips"),
    "margin_chg":    ("融資餘額增減", -1, "chips"),
    # 技術
    "rs_ratio":      ("相對強度 RS-Ratio", 1, "technical"),
    "rs_momentum":   ("相對強度動能 RS-Momentum", 1, "technical"),
    "excess_20":     ("20日超額報酬", 1, "technical"),
    "excess_60":     ("60日超額報酬", 1, "technical"),
    "excess_120":    ("120日超額報酬", 1, "technical"),
}

# 三種期間各自的指標權重。加總為 1。
HORIZONS = {
    "short": {
        "label": "短期",
        "range": "1～3 個月",
        "note": "這段期間沒有新的財報資訊，所以以籌碼流向與價格動能為主。"
                "基本面只取每月更新的營收動能，不做獲利預測。",
        "weights": {
            "rev_accel": 0.10, "rev_yoy": 0.05,
            "foreign_flow": 0.20, "trust_flow": 0.15, "margin_chg": 0.10,
            "rs_momentum": 0.25, "excess_20": 0.15,
        },
    },
    "medium": {
        "label": "中期",
        "range": "3 個月～2 年",
        "note": "基本面主導。用季報獲利品質與月營收動能判斷產業景氣位置，"
                "籌碼與技術面只作為「市場是否已經認同」的驗證。",
        "weights": {
            "rev_yoy": 0.14, "rev_accel": 0.12, "rev_breadth": 0.08,
            "op_margin": 0.11, "net_margin": 0.06, "roe": 0.09,
            "pe": 0.05, "pb": 0.05,
            "foreign_flow": 0.10, "trust_flow": 0.05, "margin_chg": 0.03,
            "rs_ratio": 0.07, "excess_60": 0.05,
        },
    },
    "long": {
        "label": "長期",
        "range": "2 年以上",
        "note": "只看產業體質與價格是否合理。刻意不納入籌碼面與短期技術面，"
                "因為那些訊號在兩年尺度上沒有預測價值。",
        "weights": {
            "roe": 0.22, "op_margin": 0.16, "net_margin": 0.10,
            "rev_yoy_cum": 0.12, "profit_breadth": 0.10,
            "pe": 0.10, "pb": 0.08, "div_yield": 0.07,
            "excess_120": 0.05,
        },
    },
}


# ---------------------------------------------------------------- 工具
def load(name: str):
    return json.loads((SNAP / f"{name}.json").read_text(encoding="utf-8"))


def robust_z(values: dict[str, float]) -> dict[str, float]:
    """跨產業的穩健標準化：(x - 中位數) / (1.4826 * MAD)，裁切到 ±2.5。

    用 MAD 而不是標準差，是因為台股常出現單一產業獲利暴衝（航運、記憶體），
    用標準差會讓其他 30 個產業全部擠成一團看不出差別。
    """
    xs = [v for v in values.values() if v is not None and math.isfinite(v)]
    if len(xs) < 3:
        return {k: 0.0 for k in values}
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    scale = 1.4826 * mad if mad > 0 else (statistics.pstdev(xs) or 1.0)
    out = {}
    for k, v in values.items():
        if v is None or not math.isfinite(v):
            out[k] = None
        else:
            out[k] = max(-2.5, min(2.5, (v - med) / scale))
    return out


def safe_div(a, b):
    return a / b if b else None


def pct_rank(values: dict[str, float]) -> dict[str, float]:
    ranked = sorted((v, k) for k, v in values.items())
    n = len(ranked)
    return {k: round(100 * i / (n - 1), 1) if n > 1 else 50.0
            for i, (_, k) in enumerate(ranked)}


# ---------------------------------------------------------------- 個股層級
def build_stocks() -> dict[str, dict]:
    """把各張表併成 個股代號 -> 指標 的字典。"""
    stocks: dict[str, dict] = {}

    for r in load("monthly_revenue"):
        ind = r.get("產業別", "").strip()
        if ind in EXCLUDED_INDUSTRY:
            continue
        cur = twse.num(r.get("營業收入-當月營收"))
        prev_y = twse.num(r.get("營業收入-去年當月營收"))
        cum = twse.num(r.get("累計營業收入-當月累計營收"))
        cum_y = twse.num(r.get("累計營業收入-去年累計營收"))
        stocks[r["公司代號"]] = {
            "code": r["公司代號"], "name": r.get("公司名稱", "").strip(),
            "industry": ind,
            "rev": cur, "rev_prev_y": prev_y, "rev_cum": cum, "rev_cum_y": cum_y,
            "rev_yoy": twse.num(r.get("營業收入-去年同月增減(%)")),
            "rev_month": r.get("資料年月"),
        }

    # 季報：營收 / 營益 / 淨利 / EPS（各產業統一格式）
    #
    # 注意：證交所這張表的「季別」是「累計至第 N 季」，不是單季。
    # 已用台積電對帳確認：115Q2 營收 2.40 兆 ≈ 月營收前 7 月累計 2.87 兆 × 6/7。
    # 所以年化淨利要乘 4/N，不是固定乘 4。
    for r in load("industry_eps"):
        s = stocks.get(r["公司代號"])
        if s is None:
            continue
        quarter = int(twse.num(r.get("季別"), 0) or 0)
        s.update({
            "q_rev": twse.num(r.get("營業收入")),
            "q_op": twse.num(r.get("營業利益")),
            "q_net": twse.num(r.get("稅後淨利")),
            "eps": twse.num(r.get("基本每股盈餘(元)")),
            "q_quarter": quarter,
            "q_period": f"{r.get('年度')}Q{r.get('季別')}",
        })

    # 毛利（只有一般業有）
    for r in load("income_general"):
        s = stocks.get(r["公司代號"])
        if s is not None:
            s["q_gross"] = twse.num(r.get("營業毛利（毛損）淨額")
                                    or r.get("營業毛利（毛損）"))

    # 股本與權益
    for table in ("balance_general", "balance_finance", "balance_holding",
                  "balance_insurance", "balance_broker", "balance_other"):
        try:
            rows = load(table)
        except FileNotFoundError:
            continue
        for r in rows:
            s = stocks.get(r.get("公司代號", ""))
            if s is None:
                continue
            capital = twse.num(r.get("股本"))
            s["capital"] = capital
            # 股本單位是千元、面額 10 元 -> 股數 = 股本 * 100
            s["shares"] = capital * 100 if capital else None
            s["equity"] = twse.num(r.get("權益總計"))

    for r in load("valuation"):
        s = stocks.get(r.get("Code", ""))
        if s is not None:
            s["pe"] = twse.num(r.get("PEratio"))
            s["pb"] = twse.num(r.get("PBratio"))
            s["div_yield"] = twse.num(r.get("DividendYield"))

    for r in load("daily_quote"):
        s = stocks.get(r.get("Code", ""))
        if s is not None:
            s["close"] = twse.num(r.get("ClosingPrice"))

    for r in load("margin"):
        s = stocks.get(str(r.get("股票代號", "")).strip())
        if s is not None:
            s["margin_now"] = twse.num(r.get("融資今日餘額"), 0.0)
            s["margin_prev"] = twse.num(r.get("融資前日餘額"), 0.0)

    for s in stocks.values():
        s["market"] = "twse"

    _merge_otc(stocks)
    return stocks


def _merge_otc(stocks: dict[str, dict]) -> None:
    """把上櫃公司併進同一組產業。

    上櫃只有月營收、估值、股本、融資券——季報在櫃買與證交所的 OpenAPI 都沒有
    開放端點。所以上櫃公司會把產業的營收動能拉動，但營益率 / ROE 仍然只由
    上市公司算出來。這個落差會寫進輸出，前端要顯示。
    """
    try:
        rev = load("otc_monthly_revenue")
    except FileNotFoundError:
        return

    for r in rev:
        code = r["公司代號"]
        if code in stocks:      # 同代號不會同時上市又上櫃，保險起見
            continue
        stocks[code] = {
            "code": code, "name": r.get("公司名稱", "").strip(),
            "industry": r.get("產業別", "其他"),
            "market": "otc",
            "rev": r.get("營業收入-當月營收"),
            "rev_prev_y": r.get("營業收入-去年當月營收"),
            "rev_cum": r.get("累計營業收入-當月累計營收"),
            "rev_cum_y": r.get("累計營業收入-去年累計營收"),
            "rev_yoy": r.get("營業收入-去年同月增減(%)"),
            "rev_month": r.get("資料年月"),
        }

    for name, key_map in (
        ("otc_valuation", {"pe": "PriceEarningRatio", "pb": "PriceBookRatio",
                           "div_yield": "YieldRatio"}),
        ("otc_quote", {"close": "Close", "capital": "Capitals"}),
    ):
        try:
            rows = load(name)
        except FileNotFoundError:
            continue
        for r in rows:
            s = stocks.get(str(r.get("SecuritiesCompanyCode", "")).strip())
            if s is None or s.get("market") != "otc":
                continue
            for field, src in key_map.items():
                s[field] = twse.num(r.get(src))
        # 股本單位同上市：千元、面額 10 元 -> 股數 = 股本 * 100
        if name == "otc_quote":
            for s in stocks.values():
                if s.get("market") == "otc" and s.get("capital"):
                    s["shares"] = s["capital"] * 100

    try:
        rows = load("otc_margin")
    except FileNotFoundError:
        return
    for r in rows:
        s = stocks.get(str(r.get("SecuritiesCompanyCode", "")).strip())
        if s is not None and s.get("market") == "otc":
            s["margin_now"] = twse.num(r.get("MarginPurchaseBalance"), 0.0)
            s["margin_prev"] = twse.num(r.get("MarginPurchaseBalancePreviousDay"), 0.0)


# ---------------------------------------------------------------- 技術面
def load_index_history() -> dict[str, list[tuple[str, float]]]:
    path = HIST / "index_history.csv"
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            series[normalize_index_name(row["index_name"])].append(
                (row["date"], float(row["close"])))
    for name in series:
        series[name].sort()
    return series


def _rolling_norm(xs: list[float], w: int) -> list[float | None]:
    """滾動標準化成以 100 為中心，這是 RRG 的慣例刻度。"""
    out: list[float | None] = []
    for i in range(len(xs)):
        if i + 1 < w:
            out.append(None)
            continue
        win = xs[i + 1 - w: i + 1]
        m = statistics.fmean(win)
        sd = statistics.pstdev(win)
        out.append(100 + (xs[i] - m) / sd if sd > 0 else 100.0)
    return out


def rrg(sector: list[float], bench: list[float], w: int = 60):
    """JdK RS-Ratio / RS-Momentum。回傳最新一組座標與軌跡。"""
    if len(sector) < w + 15 or len(bench) < w + 15:
        return None
    rs = [100 * s / b for s, b in zip(sector, bench) if b]
    rsr = _rolling_norm(rs, w)
    valid = [(i, v) for i, v in enumerate(rsr) if v is not None]
    if len(valid) < 15:
        return None

    vals = [v for _, v in valid]
    lag = max(1, w // 6)
    mom = [100 * vals[i] / vals[i - lag] if i >= lag and vals[i - lag] else None
           for i in range(len(vals))]
    mom_ok = [m for m in mom if m is not None]
    if len(mom_ok) < 12:
        return None
    rsm = _rolling_norm(mom_ok, min(w, len(mom_ok)))

    offset = len(vals) - len(rsm)
    # 軌跡存成扁平陣列 [x1,y1,x2,y2,...]，比一堆 {"x":..,"y":..} 省一半體積
    tail: list[float] = []
    for k in range(max(0, len(rsm) - 10), len(rsm)):
        if rsm[k] is None:
            continue
        tail.extend((round(vals[offset + k], 2), round(rsm[k], 2)))
    if not tail:
        return None
    return {"rs_ratio": tail[-2], "rs_momentum": tail[-1], "tail": tail}


def quadrant(x: float, y: float) -> str:
    if x >= 100 and y >= 100:
        return "領先"
    if x >= 100:
        return "轉弱"
    if y >= 100:
        return "改善"
    return "落後"


def excess_return(sector: list[float], bench: list[float], n: int):
    if len(sector) <= n or len(bench) <= n:
        return None
    s = safe_div(sector[-1], sector[-1 - n])
    b = safe_div(bench[-1], bench[-1 - n])
    return None if s is None or b is None else round(100 * (s - b), 2)


# ---------------------------------------------------------------- 籌碼面
def load_chips(stocks: dict[str, dict], days: int = 20):
    path = HIST / "chips.csv"
    if not path.exists():
        return {}, 0
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    if not rows:
        return {}, 0
    dates = sorted({r["date"] for r in rows})[-days:]
    window = set(dates)

    net: dict[str, dict[str, float]] = defaultdict(
        lambda: {"foreign": 0.0, "trust": 0.0})
    for r in rows:
        if r["date"] not in window:
            continue
        net[r["industry"]]["foreign"] += float(r["foreign_net"])
        net[r["industry"]]["trust"] += float(r["trust_net"])

    # 分母只算上市股數：三大法人的日資料來自證交所 T86，本身就只含上市。
    # 若分母混入上櫃股本，買超佔比會被系統性稀釋。
    shares_by_ind: dict[str, float] = defaultdict(float)
    for s in stocks.values():
        if s.get("shares") and s.get("market") == "twse":
            shares_by_ind[s["industry"]] += s["shares"]

    out = {}
    for ind, vals in net.items():
        total = shares_by_ind.get(ind)
        if not total:
            continue
        out[ind] = {
            "foreign_flow": round(100 * vals["foreign"] / total, 4),
            "trust_flow": round(100 * vals["trust"] / total, 4),
        }
    return out, len(dates)


# ---------------------------------------------------------------- 產業彙總
def aggregate(stocks: dict[str, dict], index_hist, chips) -> dict[str, dict]:
    by_ind: dict[str, list[dict]] = defaultdict(list)
    for s in stocks.values():
        by_ind[s["industry"]].append(s)

    index_names = set(index_hist)
    bench_by_date = {d: c for d, c in index_hist.get(BENCHMARK_INDEX, [])}

    sectors = {}
    for ind, members in by_ind.items():
        idx_name = resolve(ind, index_names)
        raw: dict[str, float | None] = {}

        # --- 基本面：月營收（加總後再算比率，避免小公司噪音放大）
        rev = sum(m["rev"] for m in members if m.get("rev"))
        rev_y = sum(m["rev_prev_y"] for m in members if m.get("rev_prev_y"))
        cum = sum(m["rev_cum"] for m in members if m.get("rev_cum"))
        cum_y = sum(m["rev_cum_y"] for m in members if m.get("rev_cum_y"))
        raw["rev_yoy"] = round(100 * (rev / rev_y - 1), 2) if rev_y else None
        raw["rev_yoy_cum"] = round(100 * (cum / cum_y - 1), 2) if cum_y else None
        if raw["rev_yoy"] is not None and raw["rev_yoy_cum"] is not None:
            raw["rev_accel"] = round(raw["rev_yoy"] - raw["rev_yoy_cum"], 2)
        else:
            raw["rev_accel"] = None
        yoys = [m["rev_yoy"] for m in members if m.get("rev_yoy") is not None]
        raw["rev_breadth"] = (round(100 * sum(1 for y in yoys if y > 0) / len(yoys), 1)
                              if yoys else None)

        # --- 基本面：季報
        weak = ind in WEAK_QUARTERLY
        q_rev = sum(m["q_rev"] for m in members if m.get("q_rev"))
        q_op = sum(m["q_op"] for m in members if m.get("q_op"))
        q_net = sum(m["q_net"] for m in members if m.get("q_net"))
        eps_list = [m["eps"] for m in members if m.get("eps") is not None]

        # ROE 的分子分母必須來自同一組公司，否則有淨利但缺資產負債表的
        # 公司會讓分子偏大、分母偏小，把整個產業的 ROE 灌上去
        roe_pairs = [(m["q_net"], m["equity"]) for m in members
                     if m.get("q_net") is not None and m.get("equity")]
        roe_net = sum(p[0] for p in roe_pairs)
        equity = sum(p[1] for p in roe_pairs)

        # 累計期數：Q2 的數字是半年報，年化要乘 4/2 而不是 4
        quarters = [m["q_quarter"] for m in members if m.get("q_quarter")]
        cum_q = statistics.mode(quarters) if quarters else 4
        annualize = 4 / cum_q

        raw["op_margin"] = (round(100 * q_op / q_rev, 2)
                            if q_rev and not weak else None)
        raw["net_margin"] = (round(100 * q_net / q_rev, 2)
                             if q_rev and not weak else None)
        raw["roe"] = (round(100 * roe_net * annualize / equity, 2)
                      if equity and not weak else None)
        raw["profit_breadth"] = (
            round(100 * sum(1 for e in eps_list if e > 0) / len(eps_list), 1)
            if eps_list and not weak else None)

        # --- 估值：用中位數，避開個股極端值
        for key in ("pe", "pb", "div_yield"):
            vals = [m[key] for m in members
                    if m.get(key) is not None and m[key] > 0]
            raw[key] = round(statistics.median(vals), 2) if vals else None

        # --- 籌碼
        flow = chips.get(ind, {})
        raw["foreign_flow"] = flow.get("foreign_flow")
        raw["trust_flow"] = flow.get("trust_flow")
        m_now = sum(m.get("margin_now") or 0 for m in members)
        m_prev = sum(m.get("margin_prev") or 0 for m in members)
        raw["margin_chg"] = round(100 * (m_now / m_prev - 1), 3) if m_prev else None

        # --- 技術面 / RRG
        # 用日期交集對齊，不能只取最後 N 筆：某檔類指數若缺了某個交易日，
        # 位置對齊會讓整條序列錯位，算出來的相對強度就整個是錯的。
        rrg_data = None
        if idx_name and bench_by_date:
            sec_s, ben_s = [], []
            for day, close in index_hist[idx_name]:
                b = bench_by_date.get(day)
                if b:
                    sec_s.append(close)
                    ben_s.append(b)
            rrg_data = rrg(sec_s, ben_s, w=60)
            if rrg_data:
                raw["rs_ratio"] = rrg_data["rs_ratio"]
                raw["rs_momentum"] = rrg_data["rs_momentum"]
            for horizon in (20, 60, 120):
                raw[f"excess_{horizon}"] = excess_return(sec_s, ben_s, horizon)

        n_twse = sum(1 for m in members if m.get("market") == "twse")
        n_otc = len(members) - n_twse
        sectors[ind] = {
            "industry": ind,
            "index_name": idx_name,
            "members": len(members),
            "members_twse": n_twse,
            "members_otc": n_otc,
            # 季報只有上市有；上櫃在兩邊 OpenAPI 都沒有開放端點
            "quarterly_from": n_twse,
            "raw": raw,
            "rrg": rrg_data,
            "weak_quarterly": weak,
            "top_stocks": pick_top_stocks(members),
        }
    return sectors


def pick_top_stocks(members: list[dict], n: int = 8) -> list[dict]:
    """挑出產業內基本面較突出的個股，給使用者當觀察名單起點。

    用三個容易解釋的維度做產業內排名：成長（營收年增）、獲利（EPS）、
    規模（營收占產業比重）。納入規模是因為只看成長率的話，小公司的
    基期效應會把權值股完全擠掉，那份名單對一般人沒有參考意義。

    名單是「往下查的起點」，不是推薦。
    """
    # 上櫃公司沒有 EPS（季報無來源），不能因此被整批排除，
    # 否則上櫃佔多數的產業會給出空名單。缺 EPS 一律以同業中位數代入。
    pool = [m for m in members if m.get("rev_yoy") is not None and m.get("rev")]
    if not pool:
        return []

    known_eps = [m["eps"] for m in pool if m.get("eps") is not None]
    eps_fallback = statistics.median(known_eps) if known_eps else 0.0
    total_rev = sum(m["rev"] for m in pool) or 1.0

    def minmax(key_fn):
        vals = [key_fn(m) for m in pool]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        return {id(m): (key_fn(m) - lo) / span if span else 0.5 for m in pool}

    growth = minmax(lambda m: max(-30.0, min(120.0, m["rev_yoy"])))
    profit = minmax(lambda m: max(-3.0, min(
        25.0, m["eps"] if m.get("eps") is not None else eps_fallback)))
    size = minmax(lambda m: math.log10(max(m["rev"], 1.0)))

    scored = sorted(
        pool,
        key=lambda m: -(0.45 * growth[id(m)] + 0.35 * profit[id(m)]
                        + 0.20 * size[id(m)]),
    )

    return [{
        "code": m["code"], "name": m["name"],
        "market": m.get("market", "twse"),
        "rev_yoy": m.get("rev_yoy"), "eps": m.get("eps"),
        "pe": m.get("pe"), "pb": m.get("pb"),
        "div_yield": m.get("div_yield"), "close": m.get("close"),
        "rev_share": round(100 * m["rev"] / total_rev, 1),
    } for m in scored[:n]]


# ---------------------------------------------------------------- 合成分數
def score(sectors: dict[str, dict]) -> None:
    keys = list(sectors)
    z: dict[str, dict[str, float | None]] = {}
    for metric, (_, direction, _) in METRICS.items():
        vals = {k: sectors[k]["raw"].get(metric) for k in keys}
        zs = robust_z(vals)
        for k in keys:
            v = zs.get(k)
            z.setdefault(k, {})[metric] = None if v is None else v * direction

    for name, cfg in HORIZONS.items():
        composite = {}
        total_weight = sum(cfg["weights"].values())
        for k in keys:
            total, used = 0.0, 0.0
            contrib = []
            for metric, w in cfg["weights"].items():
                zv = z[k].get(metric)
                if zv is None:
                    continue
                total += zv * w
                used += w
                # 只存 [指標代號, z 值]。label / group / weight / impact / raw
                # 全部可以在前端從 metric_meta、horizons.weights 和 sector.raw
                # 推導出來，重複存會讓這份 JSON 大一倍。
                contrib.append([metric, round(zv, 2)])
            # 缺資料的指標一律當成「與同業中位數相同」（z=0），
            # 也就是除以完整權重而不是除以實際用到的權重。
            #
            # 這點很重要：如果改成把權重攤回給有資料的指標，等於假設
            # 「沒看到的那項，會跟看得到的那幾項一樣好」。金融保險業的季報
            # 全缺、只剩便宜的估值指標，攤回權重會讓它在最看重獲利品質的
            # 長期榜衝到第 3 名——把「不知道」變成了「很好」。
            composite[k] = total / total_weight if total_weight > 0 else 0.0
            contrib.sort(key=lambda c: -abs(c[1] * cfg["weights"][c[0]]))
            sectors[k].setdefault("horizons", {})[name] = {
                "raw_score": round(composite[k], 3),
                "coverage": round(100 * used / total_weight, 0),
                "contributions": contrib,
            }

        ranks = pct_rank(composite)
        ordered = sorted(keys, key=lambda k: -composite[k])
        for pos, k in enumerate(ordered, 1):
            h = sectors[k]["horizons"][name]
            h["score"] = ranks[k]
            h["rank"] = pos


def verdict(score_value: float) -> str:
    if score_value >= 80:
        return "強"
    if score_value >= 60:
        return "偏強"
    if score_value >= 40:
        return "中性"
    if score_value >= 20:
        return "偏弱"
    return "弱"


# ---------------------------------------------------------------- 輸出
def main() -> None:
    print("載入快照…")
    stocks = build_stocks()
    print(f"  個股 {len(stocks):,} 檔")

    print("載入指數歷史…")
    index_hist = load_index_history()
    days = len(index_hist.get(BENCHMARK_INDEX, []))
    print(f"  {len(index_hist)} 檔指數、{days} 個交易日")

    print("載入籌碼面…")
    chips, chip_days = load_chips(stocks)
    print(f"  {len(chips)} 個產業、近 {chip_days} 個交易日")

    print("彙總產業…")
    sectors = aggregate(stocks, index_hist, chips)
    score(sectors)

    rev_month = next((s.get("rev_month") for s in stocks.values()
                      if s.get("rev_month")), None)
    q_period = next((s.get("q_period") for s in stocks.values()
                     if s.get("q_period")), None)
    last_day = index_hist.get(BENCHMARK_INDEX, [("", 0)])[-1][0]

    payload = {
        "generated_at": last_day,
        "data_asof": {
            "price": last_day,
            "revenue": rev_month,
            "financials": q_period,
            "chip_days": chip_days,
            "history_days": days,
        },
        "horizons": HORIZONS,   # 含 weights，前端要靠它還原每個指標的貢獻度
        "metric_meta": {k: {"label": v[0], "direction": v[1], "group": v[2]}
                        for k, v in METRICS.items()},
        "sectors": [],
    }

    for ind, s in sorted(sectors.items(),
                         key=lambda kv: -kv[1]["horizons"]["medium"]["score"]):
        rrg_data = s["rrg"]
        payload["sectors"].append({
            "industry": ind,
            "index_name": s["index_name"],
            "members": s["members"],
            "members_twse": s["members_twse"],
            "members_otc": s["members_otc"],
            "weak_quarterly": s["weak_quarterly"],
            "raw": s["raw"],
            "rrg": rrg_data,
            "quadrant": (quadrant(rrg_data["rs_ratio"], rrg_data["rs_momentum"])
                         if rrg_data else None),
            "scores": {h: {**s["horizons"][h],
                           "verdict": verdict(s["horizons"][h]["score"])}
                       for h in HORIZONS},
            "top_stocks": s["top_stocks"],
        })

    # 精簡輸出：這份檔案是要走網路給手機讀的，縮排會讓體積多出四成
    path = OUT / "dashboard.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    size = path.stat().st_size / 1024
    print(f"\n輸出 {path}  ({size:.0f} KB)")

    print("\n中期（基本面主導）排名前 8：")
    for s in payload["sectors"][:8]:
        m = s["scores"]["medium"]
        print(f"  {m['rank']:2d}. {s['industry']:14s} {m['score']:5.1f} 分"
              f"  {m['verdict']:3s}  營收YoY={s['raw'].get('rev_yoy')}%"
              f"  營益率={s['raw'].get('op_margin')}%")


if __name__ == "__main__":
    main()
