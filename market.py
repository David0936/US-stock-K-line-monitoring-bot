"""
行情层：yfinance 免费拉美股/指数 K 线与报价（数据源做了抽象，后续可换 Finnhub/Stooq）。

- fetch_ohlc(ticker)  → 日线/分钟线 DataFrame（columns: open/high/low/close/volume，index 为时间）
- quote(ticker)       → {price, prev_close, change, change_pct, day_high, day_low, volume}
- quotes(tickers)     → 批量报价 {ticker: quote}

缓存：报价 ~60s、日线数小时，避免 yfinance 限流。批量优先用 yf.download 一次取多只。
注意：yfinance 偶发限流、需稳定外网；本模块把数据源封装在这里，换源只改此文件。
"""
import threading
import time
from datetime import datetime, time as dtime, timezone

import pandas as pd
import yfinance as yf

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # 极端环境无 tzdata 时退回固定 -4（仅夏令时近似）
    from datetime import timedelta
    ET = timezone(timedelta(hours=-4))

_lock = threading.Lock()
_cache = {}  # key -> (expire_ts, value)
_MISS = object()        # 区分"未缓存"与"缓存了 None"（负缓存）

QUOTE_TTL = 60          # 报价缓存秒数
DAILY_TTL = 3600        # 日线缓存秒数
INTRADAY_TTL = 90       # 分钟线缓存秒数（盯盘要快，压到 90s）
NEGATIVE_TTL = 120      # 空结果/失败的负缓存，避免周末/坏代码每轮直打 yfinance 被限流

# 美股全休市日（NYSE，ET 日期）。半日提前收盘暂按整日常规处理。
MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def session_now(now_utc=None):
    """按美东时间判断当前市场时段：premarket/regular/afterhours/overnight/closed。
    常规盘 9:30–16:00 ET；盘前 4:00–9:30；盘后 16:00–20:00；其余为隔夜；周末为 closed。"""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ET)
    if now.weekday() >= 5:           # 周六日
        return "closed"
    if now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:   # 美股节假日整休
        return "closed"
    t = now.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "premarket"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "regular"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "afterhours"
    return "overnight"             # 20:00–次日 04:00（个股/ETF 不交易，看期货+亚洲）


SESSION_LABEL = {
    "premarket": "盘前", "regular": "盘中", "afterhours": "盘后",
    "overnight": "隔夜", "closed": "休市",
}


def _cache_get(key):
    with _lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]            # 可能是 None（负缓存）
    return _MISS


def _cache_set(key, value, ttl):
    with _lock:
        _cache[key] = (time.time() + ttl, value)


def _cache_set_auto(key, df):
    """有结果用正常 TTL，空结果用短负缓存 TTL。"""
    is_intra = key.startswith("intraday:")
    ttl = (INTRADAY_TTL if is_intra else DAILY_TTL) if df is not None else NEGATIVE_TTL
    _cache_set(key, df, ttl)


def _normalize(df):
    """把 yfinance DataFrame 列名规范成小写 open/high/low/close/volume。"""
    if df is None or df.empty:
        return None
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].dropna(how="all")
    return df if not df.empty else None


def fetch_ohlc(ticker, period="6mo", interval="1d"):
    """取单只标的 K 线。日线默认 6 个月（够算 MA200 之外的多数指标）。"""
    key = f"ohlc:{ticker}:{period}:{interval}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
        df = _normalize(df)
    except Exception:
        df = None
    _cache_set_auto(key, df)         # 空结果也缓存(负缓存)，防限流
    return df


def fetch_intraday(ticker, interval="1m", lookback="1d", prepost=True):
    """取含盘前盘后的分钟线（美东 04:00–20:00）。缓存 ~90s。
    interval 可用 1m/2m/5m；lookback 用 yfinance period（1m 线最多 7d）。"""
    key = f"intraday:{ticker}:{interval}:{lookback}:{int(prepost)}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    try:
        df = yf.Ticker(ticker).history(period=lookback, interval=interval,
                                       prepost=prepost, auto_adjust=False)
        df = _normalize(df)
    except Exception:
        df = None
    _cache_set_auto(key, df)         # 空结果也缓存(负缓存)，防限流
    return df


def quote(ticker):
    """单只报价。优先 fast_info 取实时价（盘中有效）；拿不到退回日线收盘。"""
    key = f"quote:{ticker}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    q = _quote_fast(ticker) or _quote_from_df(fetch_ohlc(ticker, period="5d", interval="1d"))
    _cache_set(key, q, QUOTE_TTL if q else NEGATIVE_TTL)   # 空报价短负缓存
    return q


def _quote_fast(ticker):
    """用 yfinance fast_info 取实时最新价 + 昨收（盘中实时，盘后为收盘）。"""
    try:
        fi = yf.Ticker(ticker).fast_info
        price = fi.get("last_price")
        prev = fi.get("previous_close")
        if price is None or prev is None:
            return None
        price, prev = float(price), float(prev)
        chg = price - prev
        return {
            "price": round(price, 2),
            "prev_close": round(prev, 2),
            "change": round(chg, 2),
            "change_pct": round(chg / prev * 100, 2) if prev else 0.0,
            "day_high": round(float(fi.get("day_high") or price), 2),
            "day_low": round(float(fi.get("day_low") or price), 2),
            "volume": int(fi.get("last_volume") or 0),
            "asof": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return None


def _quote_from_df(df):
    if df is not None and "close" in df.columns:
        df = df.dropna(subset=["close"])     # 去掉未完成的当日 bar（Close=NaN）
    if df is None or "close" not in df.columns or len(df) < 1:
        return None
    last = df.iloc[-1]
    price = float(last["close"])
    # 只有 1 根 bar 时无法算真实涨跌，置 0 而非拿同根 open 冒充昨收
    has_prev = len(df) >= 2
    prev_close = float(df.iloc[-2]["close"]) if has_prev else price
    chg = price - prev_close
    return {
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "change": round(chg, 2),
        "change_pct": round(chg / prev_close * 100, 2) if (has_prev and prev_close) else 0.0,
        "day_high": round(float(last["high"]), 2),
        "day_low": round(float(last["low"]), 2),
        "volume": int(last["volume"]) if not pd.isna(last["volume"]) else 0,
        "asof": datetime.now(timezone.utc).isoformat(),
    }


def quotes(tickers):
    """批量报价：逐只走 quote()（自带 fast_info + 60s 缓存），标的不多、更实时。"""
    out = {}
    for t in [x for x in tickers if x]:
        q = quote(t)
        if q:
            out[t] = q
    return out


if __name__ == "__main__":
    print("当前时段:", session_now(), SESSION_LABEL.get(session_now(), ""))
    for tk in ("NVDA", "^GSPC", "QQQ"):
        print(tk, "→", quote(tk))
    df = fetch_ohlc("NVDA")
    print("NVDA 日线行数:", 0 if df is None else len(df))
    idf = fetch_intraday("NVDA", interval="1m")
    print("NVDA 分钟线行数:", 0 if idf is None else len(idf),
          ("| 区间 " + str(idf.index[0]) + " → " + str(idf.index[-1])) if idf is not None else "")
