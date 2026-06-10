"""
Stock Desk —— 美股 K 线盯盘 + 资讯联动 + AI 多空判断（Flask 后台）。

关注列表(大盘+个股) → yfinance 行情/指标 → 事件触发 → 关联新闻 → AI 多空辩论
→ 飞书推送(关键信号才推 + 每日盘前/盘后复盘) → Web 盯盘台。

独立项目，只借鉴 claworld-monitor 的飞书推送架构。AI 支持 claude / openai 兼容(中转站)切换。
"""
import functools
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)

import desk as desk_mod
import feishu
import llm
import macro as macro_mod
import market
import news as news_mod
import signals
from store import SignalStore

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULTS = {
    "AI_ENABLED": True,
    "AI_PROVIDER": "openai",          # openai 兼容 | claude 官方
    "CLAUDE_API_KEY": "",
    "CLAUDE_MODEL": "claude-opus-4-8",
    # OpenAI 兼容端点：可填官方 OpenAI、通义千问(dashscope compatible-mode)、或任意中转站
    "LLM_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "LLM_API_KEY": "",
    "LLM_MODEL": "qwen-plus",
    "FEISHU_BOTS": [],
    "SITE_URL": "",
    "DESK_ENABLED": True,
    "DESK_INDICES": ["^GSPC", "^IXIC", "QQQ"],
    "DESK_TICKERS": ["NVDA", "AMD", "TSM", "AVGO", "MU", "SMCI"],
    "DESK_LEVERAGED": ["SOXL", "SOXS", "USD", "KORU"],
    "DESK_FUTURES": ["ES=F", "NQ=F"],
    "DESK_ASIA": ["^KS11", "^N225", "^HSI", "000001.SS"],
    "UNDERLYING_MAP": {},          # {ETF: 挂钩市场代码}，覆盖 macro 默认映射
    "LEVERAGE_MAP": {},            # {ETF: 倍数}，覆盖 desk 默认
    "DESK_INTERVAL_INTRADAY": 180,  # 盘前/盘中/盘后分钟级扫描间隔(秒)
    "DESK_INTERVAL_OVERNIGHT": 600,  # 隔夜/休市扫描间隔(秒)
    "DESK_DROP_ALERT": 3.0,        # 日线单日急跌阈值%
    "DESK_FAST_DROP_PCT": 1.5,     # 日内窗口涨速阈值%(杠杆按倍数放大)
    "DESK_FAST_WINDOW_MIN": 15,
    "DESK_OVERNIGHT_ALERT": 2.5,   # 隔夜挂钩市场异动阈值%(触发开盘前预警)
    "DESK_PREPOST": True,          # 分钟线是否含盘前盘后
    "DESK_RSI_OB": 70,
    "DESK_RSI_OS": 30,
    "DESK_COOLDOWN": 14400,
    "DESK_NEWS_LOOKBACK_H": 24,
    "DESK_REVIEW_PREMARKET": "21:00",
    "DESK_REVIEW_POSTMARKET": "05:00",
    "HOLDINGS": {},
}

# ---- 全局状态 ----
store = SignalStore(str(DATA_DIR))
desk_status = {"running": False, "current_status": "未启动", "scanned": 0,
               "alerts_sent": 0, "last_scan": None, "next_scan": None}
desk_instance = None
desk_thread = None
_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_secrets(cfg):
    changed = False
    if not cfg.get("SECRET_KEY"):
        cfg["SECRET_KEY"] = secrets.token_hex(16)
        changed = True
    if not cfg.get("ADMIN_PASSWORD"):
        pw = secrets.token_urlsafe(9)
        cfg["ADMIN_PASSWORD"] = pw
        (DATA_DIR / "default_password.txt").write_text(pw, encoding="utf-8")
        print(f"==== 默认登录密码：{pw} （也写入 data/default_password.txt）====")
        changed = True
    if changed:
        save_config(cfg)
    return cfg


config = ensure_secrets(load_config())

app = Flask(__name__)
app.secret_key = config["SECRET_KEY"]
# 会话 Cookie 加固：HttpOnly 防 JS 读取，SameSite=Lax 阻断绝大多数跨站 POST(CSRF)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

import re as _re
_TICKER_RE = _re.compile(r"^[A-Z0-9.\^=\-]{1,12}$")


def valid_ticker(t):
    return bool(_TICKER_RE.match((t or "").strip().upper().replace(" ", "")))


def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **k):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


def to_beijing(s):
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


app.jinja_env.filters["beijing"] = to_beijing


# ---- 盯盘启停 ----
def start_desk():
    global desk_instance, desk_thread
    with _lock:
        # 用 running 标志 + 线程存活双重判断，且在锁内立即置位，
        # 关闭"标志由子线程稍后置位"造成的 TOCTOU（两次点击启动会起两个线程）
        if desk_status["running"] or (desk_thread and desk_thread.is_alive()):
            return False, "已在运行"
        cfg = load_config()
        if not (cfg.get("DESK_INDICES") or cfg.get("DESK_TICKERS") or cfg.get("DESK_LEVERAGED")):
            return False, "关注列表为空，请先在设置里添加标的"
        desk_status["running"] = True
        desk_instance = desk_mod.Desk(cfg, store, desk_status)
        desk_thread = threading.Thread(target=desk_instance.run, daemon=True)
        desk_thread.start()
        return True, "已启动"


def stop_desk():
    global desk_instance
    with _lock:
        if desk_instance:
            desk_instance.stop()
        desk_status["running"] = False
        desk_status["current_status"] = "已停止"
        return True, "已停止"


# ---- 页面路由 ----
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == load_config().get("ADMIN_PASSWORD"):
            session["auth"] = True
            return redirect(url_for("index"))
        flash("密码错误")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    """盯盘台：分组(大盘/期货/亚洲/个股/杠杆ETF)现价/涨跌/信号 + 最新信号时间线（公开可看）。"""
    cfg = load_config()
    groups = [
        ("大盘指数", cfg.get("DESK_INDICES") or []),
        ("指数期货", cfg.get("DESK_FUTURES") or []),
        ("亚洲股市", cfg.get("DESK_ASIA") or []),
        ("个股", cfg.get("DESK_TICKERS") or []),
        ("杠杆 ETF", cfg.get("DESK_LEVERAGED") or []),
    ]
    all_syms = [t for _, g in groups for t in g]
    latest = {t: store.latest_by_ticker(t) for t in all_syms}
    return render_template(
        "index.html",
        cfg=cfg, groups=groups, latest=latest, names=macro_mod.NAMES,
        signals=store.all()[:50],
        status=desk_status, session_label=market.SESSION_LABEL.get(market.session_now(), ""),
        is_admin=bool(session.get("auth")),
    )


@app.route("/ticker/<ticker>")
def ticker_detail(ticker):
    ticker = ticker.strip().upper().replace(" ", "")
    if not valid_ticker(ticker):
        return redirect(url_for("index"))
    cfg = load_config()
    sigs = store.by_ticker(ticker)[:30]
    items = news_mod.get_news(ticker, 8)
    return render_template(
        "detail.html", ticker=ticker, signals=sigs, news=items,
        status=desk_status, is_admin=bool(session.get("auth")), cfg=cfg,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    cfg = load_config()
    if request.method == "POST":
        f = request.form
        cfg["AI_ENABLED"] = f.get("AI_ENABLED") == "on"
        cfg["AI_PROVIDER"] = f.get("AI_PROVIDER", "openai")
        cfg["CLAUDE_API_KEY"] = f.get("CLAUDE_API_KEY", "").strip()
        cfg["CLAUDE_MODEL"] = f.get("CLAUDE_MODEL", "claude-opus-4-8").strip()
        cfg["LLM_URL"] = f.get("LLM_URL", "").strip()
        cfg["LLM_API_KEY"] = f.get("LLM_API_KEY", "").strip()
        cfg["LLM_MODEL"] = f.get("LLM_MODEL", "claude-sonnet-4-6").strip()
        cfg["SITE_URL"] = f.get("SITE_URL", "").strip()

        def _tickers(raw, upper=True):
            out = [x.strip() for x in (raw or "").replace("，", ",").replace(" ", ",").split(",") if x.strip()]
            return [x.upper() if upper else x for x in out]

        def _map(raw):
            """解析每行 'A=B' 为 {A:B}（代码大写）。"""
            d = {}
            for line in (raw or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip():
                        d[k.strip().upper()] = v.strip()
            return d
        cfg["DESK_INDICES"] = _tickers(f.get("DESK_INDICES", ""))
        cfg["DESK_TICKERS"] = _tickers(f.get("DESK_TICKERS", ""))
        cfg["DESK_LEVERAGED"] = _tickers(f.get("DESK_LEVERAGED", ""))
        cfg["DESK_FUTURES"] = _tickers(f.get("DESK_FUTURES", ""))
        cfg["DESK_ASIA"] = _tickers(f.get("DESK_ASIA", ""))
        cfg["UNDERLYING_MAP"] = _map(f.get("UNDERLYING_MAP", ""))
        cfg["DESK_INTERVAL_INTRADAY"] = max(60, int(f.get("DESK_INTERVAL_INTRADAY", 180) or 180))
        cfg["DESK_INTERVAL_OVERNIGHT"] = max(120, int(f.get("DESK_INTERVAL_OVERNIGHT", 600) or 600))
        cfg["DESK_DROP_ALERT"] = float(f.get("DESK_DROP_ALERT", 3.0) or 3.0)
        cfg["DESK_FAST_DROP_PCT"] = float(f.get("DESK_FAST_DROP_PCT", 1.5) or 1.5)
        cfg["DESK_OVERNIGHT_ALERT"] = float(f.get("DESK_OVERNIGHT_ALERT", 2.5) or 2.5)
        cfg["DESK_PREPOST"] = f.get("DESK_PREPOST") == "on"
        cfg["DESK_RSI_OB"] = float(f.get("DESK_RSI_OB", 70) or 70)
        cfg["DESK_RSI_OS"] = float(f.get("DESK_RSI_OS", 30) or 30)
        cfg["DESK_COOLDOWN"] = max(600, int(f.get("DESK_COOLDOWN", 14400) or 14400))
        cfg["DESK_REVIEW_PREMARKET"] = f.get("DESK_REVIEW_PREMARKET", "21:00").strip()
        cfg["DESK_REVIEW_POSTMARKET"] = f.get("DESK_REVIEW_POSTMARKET", "05:00").strip()

        # 持仓与浮亏（用于 AI 针对性提醒）：每行 "TICKER 亏损百分比"
        holdings = {}
        for line in (f.get("HOLDINGS", "") or "").splitlines():
            parts = line.replace("，", ",").replace(",", " ").split()
            if parts:
                tk = parts[0].strip().upper()
                pct = None
                if len(parts) > 1:
                    try:
                        pct = float(parts[1].replace("%", ""))
                    except Exception:
                        pct = None
                holdings[tk] = pct
        cfg["HOLDINGS"] = holdings

        # 飞书多群
        notes = f.getlist("bot_note")
        webhooks = f.getlist("bot_webhook")
        secs = f.getlist("bot_secret")
        bots = []
        for i, wh in enumerate(webhooks):
            wh = wh.strip()
            if wh:
                bots.append({
                    "note": (notes[i].strip() if i < len(notes) else ""),
                    "webhook": wh,
                    "secret": (secs[i].strip() if i < len(secs) else ""),
                })
        cfg["FEISHU_BOTS"] = bots

        if f.get("NEW_PASSWORD", "").strip():
            cfg["ADMIN_PASSWORD"] = f.get("NEW_PASSWORD").strip()
        save_config(cfg)
        # 运行中的盯盘线程热替换配置，关注列表/阈值/节奏即时生效，无需手动重启
        if desk_instance and desk_status.get("running"):
            try:
                desk_instance.reload_config(cfg)
                flash("已保存，并已即时应用到运行中的盯盘。")
            except Exception:
                flash("已保存。重启盯盘后生效。")
        else:
            flash("已保存。")
        return redirect(url_for("settings"))

    holdings_text = "\n".join(
        f"{k} {v}" if v is not None else k for k, v in (cfg.get("HOLDINGS") or {}).items()
    )
    underlying_text = "\n".join(f"{k}={v}" for k, v in (cfg.get("UNDERLYING_MAP") or {}).items())
    return render_template("settings.html", cfg=cfg, status=desk_status,
                           holdings_text=holdings_text, underlying_text=underlying_text)


# ---- API ----
def _all_symbols(cfg):
    out, seen = [], set()
    for k in ("DESK_INDICES", "DESK_FUTURES", "DESK_ASIA", "DESK_TICKERS", "DESK_LEVERAGED"):
        for t in cfg.get(k) or []:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


@app.route("/api/quotes")
def api_quotes():
    cfg = load_config()
    q = market.quotes(_all_symbols(cfg))
    return jsonify({"session": market.session_now(),
                    "session_label": market.SESSION_LABEL.get(market.session_now(), ""),
                    "quotes": q})


@app.route("/api/macro")
def api_macro():
    cfg = load_config()
    snap = macro_mod.snapshot(cfg)
    return jsonify({"snap": snap, "names": macro_mod.NAMES})


@app.route("/api/spark")
def api_spark():
    """批量返回各标的近 ~30 日收盘，用于看板 mini 走势线（走已缓存日线，省请求）。"""
    cfg = load_config()
    out = {}
    for t in _all_symbols(cfg):
        df = market.fetch_ohlc(t, period="2mo", interval="1d")
        if df is not None and not df.empty and "close" in df.columns:
            closes = [round(float(x), 4) for x in df["close"].dropna().tail(30).tolist()]
            if len(closes) >= 2:
                out[t] = closes
    return jsonify(out)


@app.route("/api/ohlc/<ticker>")
def api_ohlc(ticker):
    """图表用 K 线。interval=1m/5m/1d；intraday 走 fetch_intraday（含盘前盘后）。"""
    ticker = ticker.strip().upper().replace(" ", "")
    if not valid_ticker(ticker):
        return jsonify(bars=[]), 400
    interval = request.args.get("interval", "5m")
    if interval not in ("1m", "2m", "5m", "15m", "30m", "1h", "1d", "1wk"):
        interval = "5m"
    if interval.endswith("m") or interval.endswith("h"):
        df = market.fetch_intraday(ticker, interval=interval,
                                   prepost=request.args.get("prepost", "1") != "0")
    else:
        df = market.fetch_ohlc(ticker, period="6mo", interval=interval)
    if df is None or df.empty or "close" not in df.columns:
        return jsonify(bars=[])
    bars = [{"t": int(ts.timestamp()), "o": round(float(r.open), 4), "h": round(float(r.high), 4),
             "l": round(float(r.low), 4), "c": round(float(r.close), 4),
             "v": int(r.volume) if r.volume == r.volume else 0}
            for ts, r in df.dropna(subset=["close"]).iterrows()]
    return jsonify(bars=bars)


@app.route("/api/status")
def api_status():
    s = dict(desk_status)
    s["beijing_last_scan"] = to_beijing(s.get("last_scan"))
    s["signal_count"] = len(store.all())
    return jsonify(s)


@app.route("/api/analyze/<ticker>", methods=["POST"])
@login_required
def api_analyze(ticker):
    """手动分析单只：算信号 + AI 多空辩论 + 推飞书。"""
    ticker = ticker.strip().upper().replace(" ", "")
    if not valid_ticker(ticker):
        return jsonify(ok=False, msg="非法标的代码"), 400
    cfg = load_config()
    push = request.args.get("push", "1") != "0"
    # 始终用独立的临时实例，避免与后台扫描线程并发读写同一批 _prev_/_last_alert 字典
    inst = desk_mod.Desk(cfg, store, desk_status)
    try:
        sig, msg = inst.analyze_one(ticker, push=push)
    except Exception as e:
        return jsonify(ok=False, msg=f"分析失败：{e}"), 500
    if not sig:
        return jsonify(ok=False, msg=msg), 400
    return jsonify(ok=True, id=sig["id"], label=sig["label"],
                   stance=(sig.get("ai") or {}).get("stance", ""),
                   msg=("已分析并推送" if push else "已分析"))


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    ok, msg = start_desk()
    return jsonify(ok=ok, msg=msg)


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    ok, msg = stop_desk()
    return jsonify(ok=ok, msg=msg)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    print(f"启动盯盘后台 http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
