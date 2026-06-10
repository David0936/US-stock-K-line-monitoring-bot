"""飞书自定义机器人推送（interactive 卡片）。盯盘信号卡：逃顶/减仓红、抄底/超卖绿、其余蓝。

架构沿用 claworld-monitor 的 feishu.py（签名 / push_card / push_all / bots_from_config），
新增 build_desk_card() 把一条盯盘信号渲染成卡片。
"""
import base64
import hashlib
import hmac
import time

import requests


def _sign(timestamp, secret):
    s = f"{timestamp}\n{secret}"
    h = hmac.new(s.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def push_card(webhook, secret, title, lark_md, template="blue", site_url="", button_text="打开盯盘台"):
    if not webhook:
        return False, "未配置 webhook"
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": lark_md}}]
    if site_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": button_text},
            "type": "primary", "url": site_url,
        }]})
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }
    if secret:
        ts = str(int(time.time()))
        card = {"timestamp": ts, "sign": _sign(ts, secret), **card}
    try:
        r = requests.post(webhook, json=card, timeout=15)
        return r.ok, f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, str(e)


def bots_from_config(cfg):
    bots = cfg.get("FEISHU_BOTS")
    if isinstance(bots, list) and bots:
        return bots
    if cfg.get("FEISHU_WEBHOOK"):
        return [{"note": "默认", "webhook": cfg["FEISHU_WEBHOOK"], "secret": cfg.get("FEISHU_SECRET", "")}]
    return []


def push_all(bots, title, content, template="blue", site_url=""):
    """推送到所有飞书群，返回 [(note, ok, info), ...]。"""
    results = []
    for b in bots or []:
        wh = (b.get("webhook") or "").strip()
        if not wh:
            continue
        ok, info = push_card(wh, (b.get("secret") or "").strip(), title, content,
                             template=template, site_url=site_url)
        results.append((b.get("note", ""), ok, info))
    return results


# 信号标签 → 卡片头色 + emoji
_LABEL_STYLE = {
    "逃顶预警": ("red", "🔺"),
    "减仓-趋势破位": ("red", "⚠️"),
    "开盘前预警": ("red", "🌃"),
    "抄底区-超卖": ("green", "🟢"),
    "持有-趋势健康": ("blue", "✅"),
    "观望-中性": ("blue", "⏸️"),
}
# AI 倾向 → emoji
_STANCE_EMOJI = {"买入": "🟢", "加仓": "🟢", "抄底": "🟢", "持有": "✅",
                 "观望": "⏸️", "减仓": "🔻", "逃顶": "🔺", "谨慎看空": "🔻"}


def _fmt_pct(v, plus=True):
    if v is None:
        return "—"
    sign = "+" if (plus and v > 0) else ""
    return f"{sign}{v}%"


def _g(d, k):
    """取值，None/缺失都回 '—'（dict.get 默认值对值为 None 无效）。"""
    v = d.get(k)
    return v if v is not None else "—"


def build_desk_card(sig):
    """把一条盯盘信号（含 tech / ai / events / quote）拼成 (title, lark_md, template)。"""
    ticker = sig.get("ticker", "")
    tech = sig.get("tech") or {}
    intra = sig.get("intraday") or {}
    ai = sig.get("ai") or {}
    events = sig.get("events") or []
    label = sig.get("label") or tech.get("label") or "观望-中性"
    lev = sig.get("leverage") or 1
    sess = sig.get("session") or ""
    template, emoji = _LABEL_STYLE.get(label, ("blue", "📊"))

    # 现价优先用日内最新；其次日线收盘
    price = intra.get("last") if intra.get("last") is not None else tech.get("close")
    if price is None:
        price = "—"
    chg = intra.get("pct_from_prevclose") if intra.get("pct_from_prevclose") is not None else tech.get("chg_pct")
    chg_color = "red" if (chg or 0) >= 0 else "green"   # 按中文习惯：涨红跌绿
    lev_tag = f"　<font color='grey'>{lev}x杠杆</font>" if lev and lev > 1 else ""
    sess_tag = f"　<font color='grey'>[{sess}]</font>" if sess else ""

    parts = []
    parts.append(f"**{ticker}**{lev_tag}{sess_tag}　现价 **{price}**　<font color='{chg_color}'>{_fmt_pct(chg)}</font>")
    if events:
        parts.append("📡 触发：" + "；".join(e.get("text", "") for e in events))

    # 日内分钟面
    if intra:
        parts.append(
            f"日内：距开盘 {_fmt_pct(intra.get('pct_from_open'))}　VWAP {_g(intra,'vwap')}"
            f"（{'上' if intra.get('above_vwap') else '下'} {_fmt_pct(intra.get('vwap_dist'))}）　"
            f"近15分 {_fmt_pct(intra.get('vel_15m'))}　量脉冲 {_g(intra,'vol_pulse')}×"
        )

    # 日线技术快照
    if tech:
        parts.append(
            f"日线：RSI **{_g(tech,'rsi')}**（{tech.get('rsi_zone','')}）　"
            f"{tech.get('trend','')}　MACD {tech.get('macd_state','')}　"
            f"距MA50 {_fmt_pct(tech.get('ext50'))}　回撤 {_fmt_pct(tech.get('drawdown'), plus=False)}"
        )

    # 跨市场宏观
    if sig.get("macro"):
        parts.append("🌐 " + str(sig["macro"]).replace("\n", "　"))

    # AI 多空辩论结论
    stance = ai.get("stance", "")
    if stance:
        se = _STANCE_EMOJI.get(stance, "📊")
        conf = ai.get("confidence", "")
        parts.append(f"{se} **AI 倾向：{stance}**　<font color='grey'>置信度 {conf}</font>")
    if ai.get("summary"):
        parts.append("🧭 " + ai["summary"])
    if ai.get("bull"):
        parts.append("🐂 <font color='red'>多头</font>：" + ai["bull"])
    if ai.get("bear"):
        parts.append("🐻 <font color='green'>空头</font>：" + ai["bear"])
    if ai.get("levels"):
        parts.append("🎯 关键位：" + ai["levels"])
    if ai.get("risk"):
        parts.append("⚠️ 风险：" + ai["risk"])
    if ai.get("tip"):
        parts.append("💡 新手提醒：" + ai["tip"])

    parts.append("<font color='grey'>⚠️ 仅信息提示，非投资建议，请自行判断与风控。</font>")

    title = f"{emoji} {ticker} · {label}" + (f"｜AI:{stance}" if stance else "")
    return title, "\n\n".join(parts), template


def build_review_card(phase, items, site_url="", macro_text=""):
    """每日复盘卡：phase=盘前预判/盘后复盘；items=[(ticker, snap), ...]。"""
    emoji = "🌅" if "盘前" in phase else "🌙"
    parts = [f"**{phase}** · 大盘与重点个股一览"]
    if macro_text:
        parts.append("🌐 " + str(macro_text).replace("\n", "　"))
    for ticker, snap in items:
        if not snap:
            parts.append(f"• **{ticker}**：数据暂缺")
            continue
        parts.append(
            f"• **{ticker}**　{snap.get('close','—')}　{_fmt_pct(snap.get('chg_pct'))}　"
            f"RSI {snap.get('rsi','—')}　[{snap.get('label','')}]"
        )
    parts.append("<font color='grey'>⚠️ 仅信息提示，非投资建议，请自行判断与风控。</font>")
    return f"{emoji} 美股{phase}", "\n\n".join(parts), "purple"
