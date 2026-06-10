"""
跨市场宏观上下文：指数期货 + 亚洲股市，喂给 AI 做"新闻/隔夜盘↔个股"联动判断。

snapshot(cfg)              → {futures:{sym:quote}, asia:{sym:quote}}（带中文名与涨跌）
context_text(cfg, focus)   → 紧凑中文文本，喂进 AI 提示；focus 标的若有挂钩市场则重点标注
underlying_of(cfg, ticker) → 该杠杆ETF/个股挂钩的标的市场代码（KORU→^KS11 等）

隔夜（美东 20:00–次日 04:00）个股/ETF 不交易，用期货 + 亚洲盘判方向——正是 KORU 那种
"美股开盘前用韩国盘预警"的场景。数据仍走 market.quote（免费 yfinance）。
"""
import market

# 代码 → 中文名（含国家），用于人话呈现
NAMES = {
    "ES=F": "标普500期货", "NQ=F": "纳指100期货", "YM=F": "道指期货", "RTY=F": "罗素2000期货",
    "^KS11": "韩国KOSPI", "^N225": "日经225", "^HSI": "恒生指数", "000001.SS": "上证指数",
    "^TWII": "台湾加权", "^SOX": "费城半导体", "SOXX": "半导体ETF",
}

# 杠杆ETF/个股 → 挂钩市场（默认；config.UNDERLYING_MAP 可覆盖）
DEFAULT_UNDERLYING = {
    "KORU": "^KS11", "EWY": "^KS11",
    "SOXL": "^SOX", "SOXS": "^SOX", "USD": "^SOX", "SOXX": "^SOX",
    "TQQQ": "^IXIC", "SQQQ": "^IXIC", "QQQ": "^IXIC",
    "SPXL": "^GSPC", "SPXS": "^GSPC", "UPRO": "^GSPC",
    "NVDL": "NVDA", "NVDU": "NVDA", "TSLL": "TSLA", "AMDL": "AMD",
    "YINN": "^HSI", "YANG": "^HSI", "FXI": "^HSI",
}


def underlying_of(cfg, ticker):
    m = dict(DEFAULT_UNDERLYING)
    m.update((cfg or {}).get("UNDERLYING_MAP") or {})
    return m.get(ticker)


def _arrow(pct):
    return "▲" if (pct or 0) >= 0 else "▼"


def snapshot(cfg):
    futures = (cfg or {}).get("DESK_FUTURES") or ["ES=F", "NQ=F"]
    asia = (cfg or {}).get("DESK_ASIA") or ["^KS11", "^N225", "^HSI", "000001.SS"]
    return {
        "futures": {s: market.quote(s) for s in futures},
        "asia": {s: market.quote(s) for s in asia},
    }


def _line(sym, q):
    if not q:
        return f"{NAMES.get(sym, sym)} 数据暂缺"
    return f"{NAMES.get(sym, sym)} {q['price']} {_arrow(q['change_pct'])}{q['change_pct']:+.2f}%"


def context_text(cfg, focus=None):
    """喂 AI 的宏观文本。focus 为当前分析标的，若有挂钩市场则置顶强调。"""
    snap = snapshot(cfg)
    parts = []
    fut = "；".join(_line(s, q) for s, q in snap["futures"].items())
    asia = "；".join(_line(s, q) for s, q in snap["asia"].items())
    if fut:
        parts.append("指数期货：" + fut)
    if asia:
        parts.append("亚洲股市：" + asia)
    if focus:
        und = underlying_of(cfg, focus)
        if und:
            q = market.quote(und)
            if q:
                parts.insert(0, f"【{focus} 挂钩 {NAMES.get(und, und)}】当前 {q['price']} "
                                f"{_arrow(q['change_pct'])}{q['change_pct']:+.2f}% —— 重点参考其方向")
    return "\n".join(parts) if parts else "（暂无跨市场数据）"


if __name__ == "__main__":
    cfg = {}
    print(context_text(cfg, focus="KORU"))
    print("---")
    print(context_text(cfg, focus="SOXL"))
