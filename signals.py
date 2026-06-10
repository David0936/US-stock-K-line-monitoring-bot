"""
技术信号引擎（纯 pandas，不引入 TA-Lib）。

analyze(df)         → TechSnapshot：现价、各均线、RSI、MACD、布林、ATR、量比、
                      距 52 周高低 %、距近期高点回撤 %、趋势/动量/乖离判断、粗信号标签。
detect_events(prev, snap, cfg) → 触发事件列表（急跌/破位/超买超卖/金死叉/新高新低/放量）。

粗信号标签（label）：
  逃顶预警 / 减仓-趋势破位 / 持有-趋势健康 / 观望-中性 / 抄底区-超卖
"""
import pandas as pd


# ---------- 基础指标 ----------
def _sma(s, n):
    return s.rolling(n).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s, n=14):
    delta = s.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _macd(s, fast=12, slow=26, sig=9):
    macd = _ema(s, fast) - _ema(s, slow)
    signal = _ema(macd, sig)
    return macd, signal, macd - signal


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _f(x, nd=2):
    try:
        if x is None or pd.isna(x):
            return None
        return round(float(x), nd)
    except Exception:
        return None


def analyze(df):
    """对日线 DataFrame 算全套指标，返回 TechSnapshot(dict)。数据不足返回 None。"""
    if df is None or "close" not in df.columns or len(df) < 30:
        return None
    df = df.dropna(subset=["close"])   # 丢掉"今日正在形成中"的未完成日线（Close=NaN）
    if len(df) < 30:
        return None
    c = df["close"]
    close = float(c.iloc[-1])
    prev_close = float(c.iloc[-2])
    chg_pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0

    ma20 = _sma(c, 20).iloc[-1]
    ma50 = _sma(c, 50).iloc[-1] if len(c) >= 50 else None
    ma200 = _sma(c, 200).iloc[-1] if len(c) >= 200 else None
    rsi = _rsi(c).iloc[-1]
    macd, sigl, hist = _macd(c)
    macd_now, macd_prev = hist.iloc[-1], hist.iloc[-2]
    mid = _sma(c, 20)
    std = c.rolling(20).std()
    boll_up = (mid + 2 * std).iloc[-1]
    boll_dn = (mid - 2 * std).iloc[-1]
    atr = _atr(df).iloc[-1]
    vol = float(df["volume"].iloc[-1])
    vol_avg = float(df["volume"].rolling(20).mean().iloc[-1] or 0)
    vol_ratio = (vol / vol_avg) if vol_avg else 0.0

    win = c.tail(252)                       # 约 52 周
    hi_52 = float(win.max())
    lo_52 = float(win.min())
    recent_high = float(c.tail(60).max())   # 近 3 个月高点
    drawdown = (close - recent_high) / recent_high * 100 if recent_high else 0.0

    def dist(v):
        return _f((close - v) / v * 100) if v else None

    above20 = bool(ma20 is not None and close > ma20)
    above50 = bool(ma50 is not None and close > ma50)
    above200 = bool(ma200 is not None and close > ma200)
    bull_stack = bool(ma50 and ma200 and ma20 and ma20 > ma50 > ma200)
    bear_stack = bool(ma50 and ma200 and ma20 and ma20 < ma50 < ma200)
    ext50 = dist(ma50)   # 距 MA50 乖离 %
    ext200 = dist(ma200)

    # 趋势 / 动量 / 乖离
    if bull_stack:
        trend = "多头排列"
    elif bear_stack:
        trend = "空头排列"
    elif above50:
        trend = "偏多"
    else:
        trend = "偏空"

    rsi_v = _f(rsi)
    if rsi_v is None:
        rsi_zone = "—"
    elif rsi_v >= 70:
        rsi_zone = "超买"
    elif rsi_v <= 30:
        rsi_zone = "超卖"
    else:
        rsi_zone = "中性"

    macd_state = "金叉向上" if macd_now > 0 else "死叉向下"

    # 粗信号标签：逆周期优先（逃顶 / 抄底）
    label = "观望-中性"
    if rsi_zone == "超买" and ext50 is not None and ext50 > 12:
        label = "逃顶预警"
    elif (rsi_zone == "超卖" or drawdown <= -20) and not bear_stack:
        label = "抄底区-超卖"
    elif bear_stack or (ma50 is not None and not above50 and chg_pct < 0):
        label = "减仓-趋势破位"
    elif bull_stack and above50 and rsi_zone != "超买":
        label = "持有-趋势健康"

    return {
        "close": _f(close),
        "prev_close": _f(prev_close),
        "chg_pct": _f(chg_pct),
        "ma20": _f(ma20), "ma50": _f(ma50), "ma200": _f(ma200),
        "above20": above20, "above50": above50, "above200": above200,
        "bull_stack": bull_stack, "bear_stack": bear_stack,
        "ext50": ext50, "ext200": ext200,
        "rsi": rsi_v, "rsi_zone": rsi_zone,
        "macd_hist": _f(macd_now, 3), "macd_hist_prev": _f(macd_prev, 3), "macd_state": macd_state,
        "boll_up": _f(boll_up), "boll_dn": _f(boll_dn),
        "atr": _f(atr),
        "vol_ratio": _f(vol_ratio),
        "hi_52": _f(hi_52), "lo_52": _f(lo_52),
        "pct_from_52high": _f((close - hi_52) / hi_52 * 100) if hi_52 else None,
        "pct_from_52low": _f((close - lo_52) / lo_52 * 100) if lo_52 else None,
        "drawdown": _f(drawdown),
        "trend": trend,
        "label": label,
    }


def detect_events(prev, snap, cfg):
    """对比上一次快照，返回本轮触发的事件列表 [{type, level, text}]。
    prev 可为 None（首次）。level: danger(红)/warn(橙)/good(绿)/info(蓝)。"""
    if not snap:
        return []
    events = []
    drop_th = float(cfg.get("DESK_DROP_ALERT", 3.0))
    rsi_ob = float(cfg.get("DESK_RSI_OB", 70))
    rsi_os = float(cfg.get("DESK_RSI_OS", 30))

    chg = snap.get("chg_pct") or 0
    # 急涨急跌
    if chg <= -drop_th:
        events.append({"type": "drop", "level": "danger", "text": f"单日急跌 {chg:.1f}%"})
    elif chg >= drop_th:
        events.append({"type": "pop", "level": "warn", "text": f"单日急涨 {chg:.1f}%"})

    rsi = snap.get("rsi")
    if rsi is not None:
        p_rsi = prev.get("rsi") if prev else None
        if rsi >= rsi_ob and (p_rsi is None or p_rsi < rsi_ob):
            events.append({"type": "rsi_ob", "level": "warn", "text": f"RSI 进入超买 {rsi:.0f}"})
        if rsi <= rsi_os and (p_rsi is None or p_rsi > rsi_os):
            events.append({"type": "rsi_os", "level": "good", "text": f"RSI 进入超卖 {rsi:.0f}（抄底区）"})

    # 均线破位 / 站回（以 MA50 为关键线）
    if prev is not None and snap.get("ma50") is not None:
        if prev.get("above50") and not snap.get("above50"):
            events.append({"type": "break_ma50", "level": "danger", "text": "跌破 MA50 关键支撑"})
        if not prev.get("above50") and snap.get("above50"):
            events.append({"type": "reclaim_ma50", "level": "good", "text": "重新站上 MA50"})

    # MACD 金叉/死叉
    h, hp = snap.get("macd_hist"), snap.get("macd_hist_prev")
    if h is not None and hp is not None:
        if hp <= 0 < h:
            events.append({"type": "macd_golden", "level": "good", "text": "MACD 金叉"})
        if hp >= 0 > h:
            events.append({"type": "macd_dead", "level": "warn", "text": "MACD 死叉"})

    # 创 52 周新高/新低
    pf_hi = snap.get("pct_from_52high")
    pf_lo = snap.get("pct_from_52low")
    if pf_hi is not None and pf_hi >= -0.2:
        events.append({"type": "new_high", "level": "warn", "text": "逼近/创 52 周新高（追高需谨慎）"})
    if pf_lo is not None and pf_lo <= 0.5:
        events.append({"type": "new_low", "level": "danger", "text": "逼近/创 52 周新低"})

    # 放量异动
    vr = snap.get("vol_ratio")
    if vr is not None and vr >= 2.0:
        events.append({"type": "volume", "level": "info", "text": f"放量 {vr:.1f}× 近20日均量"})

    return events


# ============ 日内（分钟级）信号 ============
def analyze_intraday(df, prev_close=None):
    """对分钟线 DataFrame 算日内快照。df 含盘前盘后。prev_close=昨日收盘（算跳空/距昨收）。
    返回 dict 或 None（数据不足）。"""
    if df is None or "close" not in df.columns or len(df) < 5:
        return None
    df = df.dropna(subset=["close"])
    if len(df) < 5:
        return None
    c = df["close"]
    last = float(c.iloc[-1])
    day_open = float(df["open"].iloc[0])
    day_high = float(df["high"].max())
    day_low = float(df["low"].min())

    # VWAP（按当段分钟线累计）
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].fillna(0)
    cum_v = float(vol.sum())
    vwap = float((tp * vol).sum() / cum_v) if cum_v > 0 else last

    # 涨速：近 5 / 15 分钟收盘变化%
    def vel(mins):
        if len(c) <= mins:
            return None
        base = float(c.iloc[-mins - 1])
        return _f((last - base) / base * 100) if base else None

    # 量能脉冲：最后一根量 vs 近 20 根中位量
    v_last = float(vol.iloc[-1])
    v_med = float(vol.tail(20).median() or 0)
    vol_pulse = _f(v_last / v_med, 1) if v_med else None

    rng = (day_high - day_low) or 1e-9
    return {
        "last": _f(last),
        "day_open": _f(day_open),
        "day_high": _f(day_high),
        "day_low": _f(day_low),
        "pct_from_prevclose": _f((last - prev_close) / prev_close * 100) if prev_close else None,
        "pct_from_open": _f((last - day_open) / day_open * 100) if day_open else None,
        "gap_pct": _f((day_open - prev_close) / prev_close * 100) if prev_close else None,
        "vwap": _f(vwap),
        "vwap_dist": _f((last - vwap) / vwap * 100) if vwap else None,
        "above_vwap": bool(last >= vwap),
        "rsi": _f(_rsi(c, 14).iloc[-1]) if len(c) >= 15 else None,
        "vel_5m": vel(5),
        "vel_15m": vel(15),
        "vol_pulse": vol_pulse,
        "range_pos": _f((last - day_low) / rng * 100),   # 0=日低 100=日高
        "bars": len(df),
    }


def detect_intraday_events(prev_state, snap, cfg, leverage=1.0):
    """日内事件。prev_state=上一轮日内快照（判 VWAP 上/下穿）。
    杠杆 ETF 用 leverage 放大阈值（3x 的 9% ≈ 标的 3%）。返回 [{type,level,text}]。"""
    if not snap:
        return []
    ev = []
    lev = max(1.0, float(leverage or 1.0))
    fast_th = float(cfg.get("DESK_FAST_DROP_PCT", 1.5)) * lev   # 窗口涨速阈值

    v5, v15 = snap.get("vel_5m"), snap.get("vel_15m")
    vel = v15 if v15 is not None else v5
    if vel is not None:
        if vel <= -fast_th:
            ev.append({"type": "fast_drop", "level": "danger", "text": f"日内急跌 {vel:.1f}%（近15分钟）"})
        elif vel >= fast_th:
            ev.append({"type": "fast_pop", "level": "warn", "text": f"日内急拉 {vel:.1f}%（近15分钟）"})

    # 跳空（开盘相对昨收）
    gap = snap.get("gap_pct")
    if gap is not None and abs(gap) >= 2.0 * lev:
        side = "高开" if gap > 0 else "低开"
        ev.append({"type": "gap", "level": "warn" if gap > 0 else "danger",
                   "text": f"跳空{side} {gap:+.1f}%"})

    # VWAP 上穿/下穿
    if prev_state is not None and snap.get("vwap") is not None:
        if prev_state.get("above_vwap") and not snap.get("above_vwap"):
            ev.append({"type": "vwap_break", "level": "danger", "text": "日内跌破 VWAP（多头转弱）"})
        if not prev_state.get("above_vwap") and snap.get("above_vwap"):
            ev.append({"type": "vwap_reclaim", "level": "good", "text": "日内收复 VWAP"})

    # 日内新低/新高（贴边）
    rp = snap.get("range_pos")
    if rp is not None:
        if rp <= 3:
            ev.append({"type": "intraday_low", "level": "danger", "text": "创日内新低"})
        elif rp >= 97:
            ev.append({"type": "intraday_high", "level": "warn", "text": "创日内新高（追高谨慎）"})

    # 放量脉冲
    vp = snap.get("vol_pulse")
    if vp is not None and vp >= 3.0:
        ev.append({"type": "vol_pulse", "level": "info", "text": f"放量脉冲 {vp:.1f}× 中位量"})

    return ev


if __name__ == "__main__":
    import market
    for tk in ("NVDA", "^GSPC"):
        df = market.fetch_ohlc(tk)
        snap = analyze(df)
        print(f"\n=== {tk} ===")
        if snap:
            print("标签:", snap["label"], "| 趋势:", snap["trend"], "| RSI:", snap["rsi"], snap["rsi_zone"])
            print("现价:", snap["close"], "涨跌:", snap["chg_pct"], "% | 距MA50:", snap["ext50"], "%")
            print("日线事件:", detect_events(None, snap, {}))
        else:
            print("数据不足")
        idf = market.fetch_intraday(tk, interval="1m")
        q = market.quote(tk)
        isnap = analyze_intraday(idf, prev_close=q.get("prev_close") if q else None)
        if isnap:
            print("日内: 距昨收", isnap["pct_from_prevclose"], "% | VWAP", isnap["vwap"],
                  "| 距VWAP", isnap["vwap_dist"], "% | vel15m", isnap["vel_15m"])
            print("日内事件:", detect_intraday_events(None, isnap, {}, leverage=1))
        else:
            print("日内数据不足")
