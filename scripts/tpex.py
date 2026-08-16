"""上櫃（TPEx）資料存取層。

上櫃的資料散在兩個地方，而且比上市殘缺：

  櫃買 OpenAPI   估值、三大法人、融資融券、個股行情  —— 有，格式是英文欄位
  公開資訊觀測站  月營收（HTML 表格，需要自己解析）    —— 有
  季報財報        兩邊都沒有開放端點                  —— 沒有

所以上櫃公司會貢獻月營收、估值、籌碼面，但季度獲利率與 ROE 只能由上市公司
撐起來。這個限制會一路傳到前端顯示。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twse  # noqa: E402

TPEX_API = "https://www.tpex.org.tw/openapi/v1"
MOPS = "https://mopsov.twse.com.tw/nas/t21"

# 櫃買的產業分類名稱與證交所不完全一致，統一對到證交所那套。
INDUSTRY_ALIAS = {
    "金融業": "金融保險業",
    "文化創意業": "其他",
    "農業科技": "其他",
    "電子商務": "電子通路業",
}


def api(path: str, *, cache: bool = True):
    return twse.fetch(f"{TPEX_API}/{path}", cache=cache)


def normalize_industry(name: str) -> str:
    name = str(name).strip()
    return INDUSTRY_ALIAS.get(name, name)


# ------------------------------------------------------------------ 月營收
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_INDUSTRY_RE = re.compile(r"產業別[：:]\s*([^<\r\n]{1,20})")


def _clean(cell: str) -> str:
    return _TAG_RE.sub("", cell).replace("&nbsp;", " ").replace("　", " ").strip()


def fetch_monthly_revenue(roc_year: int, month: int, market: str = "otc") -> list[dict]:
    """從公開資訊觀測站抓月營收彙總表。

    market: 'otc' 上櫃、'sii' 上市。

    欄位位置刻意只依賴原始營收數字，不用表上的百分比欄位——那幾欄在營收為 0
    的公司會多出一格，位置會跑掉。增減率一律自己算。
    """
    url = f"{MOPS}/{market}/t21sc03_{roc_year}_{month}_0.html"
    html = twse.fetch_text(url, encoding="big5")

    out: list[dict] = []
    industry = "其他"
    for chunk in _ROW_RE.findall(html):
        hit = _INDUSTRY_RE.search(chunk)
        if hit:
            industry = normalize_industry(hit.group(1))
            continue

        cells = [_clean(c) for c in _CELL_RE.findall(chunk)]
        if len(cells) < 9 or not re.fullmatch(r"\d{4}", cells[0] or ""):
            continue

        cur = twse.num(cells[2])
        prev_month = twse.num(cells[3])
        prev_year = twse.num(cells[4])
        cum = twse.num(cells[-4])
        cum_prev = twse.num(cells[-3])
        if cur is None:
            continue

        out.append({
            "公司代號": cells[0],
            "公司名稱": cells[1],
            "產業別": industry,
            "營業收入-當月營收": cur,
            "營業收入-上月營收": prev_month,
            "營業收入-去年當月營收": prev_year,
            "營業收入-去年同月增減(%)": (
                round(100 * (cur / prev_year - 1), 4) if prev_year else None),
            "累計營業收入-當月累計營收": cum,
            "累計營業收入-去年累計營收": cum_prev,
            "資料年月": f"{roc_year:03d}{month:02d}",
            "market": "otc" if market == "otc" else "twse",
        })
    return out


def latest_revenue_period(market: str = "otc") -> tuple[int, int] | None:
    """往回找最近一期已公告的月營收（每月 10 日前公告上個月）。"""
    import datetime as _dt

    today = _dt.date.today()
    year, month = today.year, today.month
    for _ in range(4):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        roc = year - 1911
        url = f"{MOPS}/{market}/t21sc03_{roc}_{month}_0.html"
        try:
            html = twse.fetch_text(url, encoding="big5")
        except Exception:  # noqa: BLE001
            continue
        if "公司代號" in html or "產業別" in html:
            return roc, month
    return None
