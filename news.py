"""
资讯收集层：拉某标的相关新闻，喂给 AI 做"新闻↔股价"联动判断。

默认用 yfinance 自带的 Yahoo Finance 新闻（免费、无需 key）。做了缓存与字段规范化。
数据源封装在这里，以后可加 RSS / Finnhub / 推特源，只改此文件。

get_news(ticker, limit) → [{title, publisher, link, ts}]（按时间倒序）
"""
import threading
import time
from datetime import datetime, timezone

import yfinance as yf

_lock = threading.Lock()
_cache = {}
NEWS_TTL = 900   # 新闻缓存 15 分钟


def _norm_item(it):
    """yfinance 新闻条目结构在不同版本里有差异，做一层兼容取值。"""
    c = it.get("content") if isinstance(it.get("content"), dict) else it
    title = c.get("title") or it.get("title") or ""
    pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else it.get("publisher", "")
    link = ""
    if isinstance(c.get("clickThroughUrl"), dict):
        link = c["clickThroughUrl"].get("url", "")
    link = link or (c.get("canonicalUrl") or {}).get("url", "") if isinstance(c.get("canonicalUrl"), dict) else link
    link = link or it.get("link", "")
    ts = c.get("pubDate") or it.get("providerPublishTime") or ""
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    if not title:
        return None
    return {"title": title.strip(), "publisher": pub or "", "link": link, "ts": str(ts)}


def get_news(ticker, limit=6):
    key = f"news:{ticker}"
    with _lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1][:limit]
    out = []
    try:
        raw = yf.Ticker(ticker).news or []
        for it in raw:
            n = _norm_item(it)
            if n:
                out.append(n)
    except Exception:
        out = []
    with _lock:
        _cache[key] = (time.time() + NEWS_TTL, out)
    return out[:limit]


def news_digest(ticker, limit=6):
    """把新闻拼成喂给 AI 的纯文本块（标题 + 来源）。无新闻返回空串。"""
    items = get_news(ticker, limit)
    if not items:
        return ""
    return "\n".join(f"- {n['title']}（{n['publisher']}）" for n in items)


if __name__ == "__main__":
    for tk in ("NVDA", "AAPL"):
        print(f"\n=== {tk} ===")
        for n in get_news(tk, 5):
            print(" •", n["title"], "|", n["publisher"], "|", n["ts"])
