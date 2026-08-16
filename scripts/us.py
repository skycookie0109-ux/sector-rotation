"""美股類股輪動資料層。

價格   Yahoo Finance chart API（免金鑰）— 11 檔 SPDR 類股 ETF 對 SPY
基本面 SEC XBRL frames API（免金鑰）— 全體申報公司的營收 / 淨利 / 股東權益
分類   SEC 的 SIC 產業代碼，自行對映到 11 個 GICS 類股

重要限制：SIC 不等於 GICS。SIC 是 1930 年代的分類系統，SEC 用它來標記申報人，
和 S&P 的 GICS 分類在邊界上一定會有出入（最典型的是亞馬遜在 SIC 屬零售、
GICS 也屬非必需消費，但 Alphabet 在 SIC 屬商業服務、GICS 卻是通訊服務）。
本模組的類股基本面因此是「近似」，不會和 XLK 等 ETF 的實際成分股完全一致。
價格面（RRG、超額報酬）用的是 ETF 本身，那部分是精確的。
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "snapshot"
HIST = ROOT / "data" / "history"
CACHE = ROOT / "data" / "cache"
for _d in (SNAP, HIST, CACHE):
    _d.mkdir(parents=True, exist_ok=True)

SEC_HEADERS = {
    "User-Agent": "sector-rotation-tw research contact skycookie0109@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

# 11 個 GICS 類股 -> 代表性的 SPDR 類股 ETF
SECTOR_ETF = {
    "資訊科技": "XLK",
    "健康護理": "XLV",
    "金融": "XLF",
    "非必需消費": "XLY",
    "必需消費": "XLP",
    "通訊服務": "XLC",
    "工業": "XLI",
    "能源": "XLE",
    "原物料": "XLB",
    "公用事業": "XLU",
    "房地產": "XLRE",
}
BENCHMARK = "SPY"

# SIC 代碼區間 -> 類股。由上而下比對，先命中先算，所以特例要放前面。
SIC_RULES: list[tuple[int, int, str]] = [
    (2833, 2836, "健康護理"),     # 藥品與生技
    (3826, 3826, "健康護理"),
    (3841, 3851, "健康護理"),     # 醫療器材
    (8000, 8099, "健康護理"),
    (8731, 8731, "健康護理"),
    (3570, 3579, "資訊科技"),     # 電腦與周邊
    (3670, 3679, "資訊科技"),     # 半導體與電子元件
    (7370, 7379, "資訊科技"),     # 軟體與資訊服務
    (3559, 3559, "資訊科技"),     # 半導體設備
    (3827, 3827, "資訊科技"),
    (2700, 2799, "通訊服務"),     # 出版
    (4800, 4899, "通訊服務"),     # 電信與廣播
    (7310, 7319, "通訊服務"),     # 廣告
    (7812, 7841, "通訊服務"),     # 影視
    (7900, 7999, "通訊服務"),     # 娛樂
    (6500, 6599, "房地產"),
    (6798, 6798, "房地產"),       # REITs
    (6000, 6499, "金融"),
    (6700, 6797, "金融"),
    (6799, 6799, "金融"),
    (4900, 4991, "公用事業"),
    (1200, 1399, "能源"),         # 油氣開採
    (2911, 2911, "能源"),         # 煉油
    (4610, 4619, "能源"),         # 管線
    (1000, 1099, "原物料"),       # 金屬礦業
    (1400, 1499, "原物料"),
    (2600, 2699, "原物料"),       # 紙業
    (2800, 2829, "原物料"),       # 化學（藥品已在前面攔截）
    (2840, 2899, "原物料"),
    (3200, 3399, "原物料"),       # 水泥玻璃鋼鐵
    (2000, 2199, "必需消費"),     # 食品菸草
    (5122, 5122, "必需消費"),
    (5140, 5149, "必需消費"),
    (5400, 5499, "必需消費"),     # 食品零售
    (2300, 2399, "非必需消費"),   # 成衣
    (3021, 3021, "非必需消費"),
    (3100, 3199, "非必需消費"),
    (3711, 3716, "非必需消費"),   # 汽車
    (3751, 3751, "非必需消費"),
    (5200, 5399, "非必需消費"),   # 零售
    (5500, 5799, "非必需消費"),
    (5900, 5999, "非必需消費"),
    (7000, 7099, "非必需消費"),   # 旅館
    (5800, 5899, "非必需消費"),   # 餐飲
    (1500, 1799, "工業"),         # 營建
    (3400, 3569, "工業"),         # 機械金屬製品
    (3580, 3669, "工業"),
    (3700, 3710, "工業"),
    (3720, 3750, "工業"),         # 航太
    (3760, 3799, "工業"),
    (4000, 4599, "工業"),         # 運輸
    (4700, 4799, "工業"),
    (8700, 8730, "工業"),
    (7300, 7309, "工業"),
    (7320, 7369, "工業"),
]


# SIC 與 GICS 分歧最嚴重、且公司規模夠大到會扭曲整個類股的例外。
# 不修的話，最明顯的症狀是「通訊服務」的營收成長會嚴重偏低——因為 Alphabet
# 和 Meta 的 SIC 是軟體服務（7370），會被算進資訊科技，但 XLC 這檔 ETF
# 現實上正是由這兩家主導。
CIK_OVERRIDE = {
    1652044: "通訊服務",    # Alphabet
    1326801: "通訊服務",    # Meta Platforms
    1403161: "金融",        # Visa（GICS 2023 年把支付網路移到金融）
    1141391: "金融",        # Mastercard
    1633917: "金融",        # PayPal
    320193: "資訊科技",     # Apple（SIC 3571 本來就對，保險起見固定住）
    1018724: "非必需消費",  # Amazon
    1045810: "資訊科技",    # NVIDIA
    2115436: "能源",        # ExxonMobil Holdings：SEC 把 XOM 這個代號指到新成立
                            # 的控股實體，它還沒申報過所以查不到 SIC
    1577552: "非必需消費",  # 阿里巴巴：SIC 給的是控股公司代碼，GICS 算零售
}

# 整段 SIC 就與 GICS 不一致的情況，比逐家覆寫有效率。
SIC_OVERRIDE = {
    6324: "健康護理",   # 醫療保險（UnitedHealth、Cigna、Elevance…）GICS 算健康護理
    5331: "必需消費",   # 量販店（Walmart、Costco、Target）GICS 算必需消費
    5912: "必需消費",   # 藥妝連鎖
    7389: "金融",       # 商業服務裡的支付與交易處理
}


def sic_to_sector(sic: int | None, cik: int | None = None) -> str | None:
    if cik is not None and cik in CIK_OVERRIDE:
        return CIK_OVERRIDE[cik]
    if not sic:
        return None
    if sic in SIC_OVERRIDE:
        return SIC_OVERRIDE[sic]
    for lo, hi, sector in SIC_RULES:
        if lo <= sic <= hi:
            return sector
    return None


# ------------------------------------------------------------------ 價格
def yahoo_history(symbol: str, rng: str = "3y") -> list[tuple[str, float]]:
    """回傳 [(日期, 收盤), ...]。"""
    import datetime as dt

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval=1d")
    cp = CACHE / f"yf_{symbol}_{rng}.json"
    if cp.exists():
        payload = json.loads(cp.read_text(encoding="utf-8"))
    else:
        resp = requests.get(url, headers=YF_HEADERS, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        cp.write_text(json.dumps(payload), encoding="utf-8")

    res = payload["chart"]["result"][0]
    stamps = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        out.append((dt.date.fromtimestamp(ts).isoformat(), float(close)))
    return out


def fetch_prices() -> dict[str, list[tuple[str, float]]]:
    series = {}
    for name, sym in [("__BENCH__", BENCHMARK)] + list(SECTOR_ETF.items()):
        try:
            series[name] = yahoo_history(sym)
            print(f"  [OK]   {sym:5s} {name:8s} {len(series[name])} 天")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {sym:5s} {name:8s} {type(exc).__name__}: {str(exc)[:50]}")
        time.sleep(0.8)
    return series


# ------------------------------------------------------------------ SIC 對照
def load_sic_map_multi(quarter: str, back: int = 3) -> dict[int, tuple[str, int]]:
    """合併最近幾季的 SIC 對照，涵蓋率會好很多。

    單季的批次檔只含「那一季有申報」的公司，所以年報制的外國發行人
    （阿里巴巴、豐田、三菱日聯這類 20-F 申報人）常常整季缺席。
    往前多疊幾季就能把它們補進來。已下載過的季度不會重抓。
    """
    year, q = int(quarter[:4]), int(quarter[-1])
    merged: dict[int, tuple[str, int]] = {}
    for _ in range(back):
        try:
            # 舊的資料不覆蓋新的，所以先抓到的（較新的）優先
            for cik, info in load_sic_map(f"{year}q{q}").items():
                merged.setdefault(cik, info)
        except Exception as exc:  # noqa: BLE001
            print(f"  {year}q{q} 略過（{type(exc).__name__}）")
        q -= 1
        if q == 0:
            year, q = year - 1, 4
    return merged


def load_sic_map(quarter: str) -> dict[int, tuple[str, int]]:
    """從 SEC 的季度批次檔取 cik -> (公司名, SIC)。

    只需要壓縮檔裡的 sub.txt，但 zip 不支援部分下載，所以整包抓下來後
    只留這一張表。抓過會快取，不會重複下載。
    """
    cp = CACHE / f"sec_sub_{quarter}.json"
    if cp.exists():
        return {int(k): (v[0], v[1]) for k, v in
                json.loads(cp.read_text(encoding="utf-8")).items()}

    url = ("https://www.sec.gov/files/dera/data/financial-statement-data-sets/"
           f"{quarter}.zip")
    print(f"  下載 SEC 批次檔 {quarter}.zip（約 60-130 MB，只抓一次）…")
    resp = requests.get(url, headers=SEC_HEADERS, timeout=600)
    resp.raise_for_status()

    out: dict[int, tuple[str, int]] = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open("sub.txt") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            for row in csv.DictReader(text, delimiter="\t"):
                try:
                    cik = int(row["cik"])
                    sic = int(row["sic"]) if row.get("sic") else 0
                except (ValueError, KeyError):
                    continue
                if sic:
                    out[cik] = (row.get("name", ""), sic)

    cp.write_text(json.dumps({str(k): list(v) for k, v in out.items()}),
                  encoding="utf-8")
    print(f"  取得 {len(out):,} 家公司的 SIC 分類")
    return out


# ------------------------------------------------------------------ 基本面
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]


def company_tickers() -> dict[int, tuple[str, str]]:
    """SEC 的代號對照：cik -> (ticker, 公司名)。"""
    cp = CACHE / "sec_company_tickers.json"
    if cp.exists():
        payload = json.loads(cp.read_text(encoding="utf-8"))
    else:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json",
                            headers=SEC_HEADERS, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        cp.write_text(json.dumps(payload), encoding="utf-8")

    out: dict[int, tuple[str, str]] = {}
    for row in payload.values():
        cik = int(row["cik_str"])
        # 同一家公司可能有多個代號（不同股別），保留第一個就好
        out.setdefault(cik, (row["ticker"], row.get("title", "")))
    return out


def frames(tag: str, period: str, unit: str = "USD") -> list[dict]:
    cp = CACHE / f"sec_frame_{tag}_{period}.json"
    if cp.exists():
        return json.loads(cp.read_text(encoding="utf-8")).get("data", [])
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json"
    resp = requests.get(url, headers=SEC_HEADERS, timeout=90)
    if resp.status_code != 200:
        cp.write_text(json.dumps({"data": []}), encoding="utf-8")
        return []
    cp.write_text(resp.text, encoding="utf-8")
    time.sleep(0.4)
    return resp.json().get("data", [])


def merged_frame(tags: list[str], period: str) -> dict[int, float]:
    """多個會計科目標籤合併成 cik -> 金額。先命中的標籤優先。"""
    out: dict[int, float] = {}
    for tag in tags:
        for row in frames(tag, period):
            cik = row.get("cik")
            if cik is not None and cik not in out:
                out[cik] = float(row["val"])
    return out
