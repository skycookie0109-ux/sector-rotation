"""證交所資料存取層。

兩個來源：
  openapi.twse.com.tw  最新一期快照，免金鑰、無明顯流量限制
  www.twse.com.tw      可指定日期的歷史資料，限制約每 5 秒 3 次

所有回應都寫入 data/cache/，重跑不會重複打 API。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

OPENAPI = "https://openapi.twse.com.tw/v1"
WEBAPI = "https://www.twse.com.tw/rwd/zh"

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.twse.com.tw/zh/trading/historical/mi-index.html",
})

_last_web_call = 0.0


def _throttle():
    """www.twse.com.tw 每 5 秒最多 3 次，取 2 秒間隔留安全邊際。"""
    global _last_web_call
    wait = 2.0 - (time.time() - _last_web_call)
    if wait > 0:
        time.sleep(wait)
    _last_web_call = time.time()


def _cache_path(url: str) -> Path:
    return CACHE / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")


def fetch(url: str, *, throttle: bool = False, cache: bool = True, tries: int = 3):
    """抓 JSON。cache=True 時同一 URL 只會真正打一次 API。"""
    cp = _cache_path(url)
    if cache and cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cp.unlink()  # 快取壞了就重抓

    last_err = None
    for attempt in range(tries):
        try:
            if throttle:
                _throttle()
            resp = _SESSION.get(url, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            if cache:
                cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except Exception as exc:  # noqa: BLE001 - 網路層什麼都可能丟
            last_err = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url}: {type(last_err).__name__} {last_err}")


def fetch_text(url: str, *, encoding: str = "utf-8", cache: bool = True,
               tries: int = 3) -> str:
    """抓非 JSON 的頁面（公開資訊觀測站的月營收是 Big5 編碼的 HTML 表格）。"""
    cp = _cache_path(url).with_suffix(".txt")
    if cache and cp.exists():
        return cp.read_text(encoding="utf-8")

    last_err = None
    for attempt in range(tries):
        try:
            resp = _SESSION.get(url, timeout=60)
            resp.raise_for_status()
            resp.encoding = encoding
            text = resp.text
            if cache:
                cp.write_text(text, encoding="utf-8")
            return text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url}: {type(last_err).__name__} {last_err}")


def openapi(path: str, *, cache: bool = True):
    """openapi.twse.com.tw 的最新快照。"""
    return fetch(f"{OPENAPI}/{path}", cache=cache)


def mi_index(date: str):
    """指定日期的各類價格指數。date 格式 YYYYMMDD。

    type=IND 只回指數表（約 29KB），比 type=ALL（約 3MB）省 100 倍流量。
    """
    return fetch(f"{WEBAPI}/afterTrading/MI_INDEX?date={date}&type=IND&response=json",
                 throttle=True)


def t86(date: str):
    """指定日期的三大法人買賣超（全市場個股）。"""
    return fetch(f"{WEBAPI}/fund/T86?date={date}&selectType=ALL&response=json",
                 throttle=True)


def roc_to_iso(roc: str) -> str:
    """民國日期字串轉西元。'1150814' -> '2026-08-14'"""
    roc = str(roc).strip().replace("/", "")
    if len(roc) != 7:
        raise ValueError(f"非預期的民國日期格式: {roc!r}")
    return f"{int(roc[:3]) + 1911:04d}-{roc[3:5]}-{roc[5:]}"


def num(value, default=None):
    """把 API 回傳的字串轉數字。處理千分位、空字串、'--'、全形空白。"""
    if value is None:
        return default
    s = str(value).strip().replace(",", "").replace("　", "").replace("%", "")
    if s in ("", "-", "--", "N/A", "不適用"):
        return default
    try:
        return float(s)
    except ValueError:
        return default
