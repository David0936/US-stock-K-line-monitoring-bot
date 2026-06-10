"""
AI 层：把技术指标快照 + 相关新闻，交给模型做"多头 vs 空头"辩论后给出结论。

供应商二选一（设置里切换，沿用 claworld 那套）：
  - claude  : Anthropic Messages API
  - openai  : OpenAI 兼容接口（通义千问 dashscope / 官方 OpenAI / 任意兼容中转站）

对外主入口：analyze_ticker(config, ticker, tech, news_text, holding_pct) -> dict。
用 ### 分隔符而非 JSON：模型输出含引号/换行时 JSON 易截断，分隔符免疫。
"""
import re
import time

import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _claude(config, prompt, system, max_tokens):
    key = (config.get("CLAUDE_API_KEY") or "").strip()
    model = config.get("CLAUDE_MODEL") or "claude-opus-4-8"
    if not key:
        raise RuntimeError("缺少 CLAUDE_API_KEY")
    r = requests.post(
        ANTHROPIC_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": model, "max_tokens": max_tokens, "thinking": {"type": "disabled"},
            "system": system, "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    for b in r.json().get("content", []):
        if b.get("type") == "text":
            return b["text"]
    return ""


def _openai(config, prompt, system, max_tokens):
    from openai import OpenAI  # 延迟导入

    key = (config.get("LLM_API_KEY") or "").strip()
    url = config.get("LLM_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = config.get("LLM_MODEL") or "qwen-plus"
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY")
    client = OpenAI(api_key=key, base_url=url)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        max_tokens=max_tokens, timeout=60,
    )
    return completion.choices[0].message.content or ""


def ai_complete(config, prompt, system="You are a helpful assistant.", max_tokens=1500, retries=3):
    provider = (config.get("AI_PROVIDER") or "openai").lower()
    fn = _claude if provider == "claude" else _openai
    last = ""
    for _ in range(retries):
        try:
            out = fn(config, prompt, system, max_tokens)
            if out and out.strip():
                return out.strip()
        except Exception as e:
            last = str(e)
            m = last.lower()
            if "429" in m or "rate" in m:
                time.sleep(8)
            elif "timeout" in m or "timed out" in m:
                time.sleep(4)
            elif "401" in m or "invalid api key" in m or "403" in m:
                break
            else:
                time.sleep(2)
    return f"[AI 处理失败] {last}"


SYSTEM = (
    "你是严谨的美股投研分析师，服务对象是容易追涨杀跌、总在高点买、低点割的新手。"
    "你会先分别站在【多头】和【空头】立场各陈述最强论据，再给出客观结论与可执行的关键价位。"
    "结论要敢于逆人性：价格乖离过大/超买时提示逃顶风险，恐慌深跌缩量时提示抄底区。"
    "务必把'当下新闻'、'亚洲盘/期货等跨市场方向'与'日线+日内技术形态'三者联系起来解释"
    "（例如挂钩韩国的 ETF 要看 KOSPI、半导体杠杆要看费城半导体与亚洲芯片股）。"
    "对杠杆 ETF 要强调波动放大与复利损耗、不宜长持。客观中立，不喊单，明确这是信息提示而非投资建议。"
)

PROMPT_TMPL = """对下面这只美股标的，结合【日线技术面】【日内分钟面】【跨市场宏观/亚洲盘】和【近期新闻】做多空辩论并给结论。
严格按以下格式输出，每个 ### 标记独占一行，标记之间填中文内容：
###倾向###
（只填一个词：买入 / 加仓 / 持有 / 观望 / 减仓 / 逃顶 / 抄底 / 谨慎看空。这是你的明确倾向）
###置信度###
（高 / 中 / 低）
###多头###
（多头最强论据，结合新闻与宏观，2-3 句）
###空头###
（空头最强论据，结合新闻与宏观，2-3 句）
###综合###
（综合判断：当下该怎么做、为什么。务必把新闻/亚洲盘/期货方向与技术形态联系起来，3-4 句）
###关键位###
（支撑位与压力位，给具体价格；以及"跌破X减仓/站上Y加仓"这类可执行规则；有日内 VWAP 也点出）
###风险###
（最需要警惕的风险点，1-2 句）
###新手提醒###
（针对追涨杀跌的一句忠告：现在该克制什么情绪、别做什么）

标的：{ticker}{leverage}
{holding}
【日线技术面】
{tech}

【日内分钟面】
{intraday}

【跨市场宏观 / 亚洲盘 / 期货】
{macro}

【近期相关新闻】
{news}"""

KEYMAP = {"倾向": "stance", "置信度": "confidence", "多头": "bull", "空头": "bear",
          "综合": "summary", "关键位": "levels", "风险": "risk", "新手提醒": "tip"}


def _parse_sections(s):
    out = {}
    keys = "|".join(KEYMAP.keys())
    parts = re.split(rf"#{{2,4}}\s*({keys})\s*#{{2,4}}", s)
    for i in range(1, len(parts) - 1, 2):
        k = KEYMAP.get(parts[i].strip())
        if k:
            out[k] = parts[i + 1].strip()
    return out


def _tech_text(tech):
    """把 TechSnapshot 转成喂给模型的紧凑文本。"""
    if not tech:
        return "（无技术数据）"
    def s(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"
    return (
        f"现价 {s(tech.get('close'))}，当日 {s(tech.get('chg_pct'),'%')}；趋势：{tech.get('trend','')}\n"
        f"RSI {s(tech.get('rsi'))}（{tech.get('rsi_zone','')}）；MACD：{tech.get('macd_state','')}\n"
        f"均线：MA20 {s(tech.get('ma20'))} / MA50 {s(tech.get('ma50'))} / MA200 {s(tech.get('ma200'))}；"
        f"{'站上' if tech.get('above50') else '跌破'}MA50，{'站上' if tech.get('above200') else '跌破'}MA200\n"
        f"距 MA50 乖离 {s(tech.get('ext50'),'%')}；距 52 周高点 {s(tech.get('pct_from_52high'),'%')}，"
        f"近期高点回撤 {s(tech.get('drawdown'),'%')}\n"
        f"量比 {s(tech.get('vol_ratio'),'×')}；布林上 {s(tech.get('boll_up'))} / 下 {s(tech.get('boll_dn'))}\n"
        f"系统粗判：{tech.get('label','')}"
    )


def _intraday_text(it):
    """把日内快照转成喂模型的紧凑文本。"""
    if not it:
        return "（无日内分钟数据，可能为指数或隔夜不交易）"
    def s(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"
    return (
        f"现价 {s(it.get('last'))}；距昨收 {s(it.get('pct_from_prevclose'),'%')}，距开盘 {s(it.get('pct_from_open'),'%')}，"
        f"跳空 {s(it.get('gap_pct'),'%')}\n"
        f"VWAP {s(it.get('vwap'))}（{'上方' if it.get('above_vwap') else '下方'} {s(it.get('vwap_dist'),'%')}）；"
        f"日内RSI {s(it.get('rsi'))}\n"
        f"近5分钟 {s(it.get('vel_5m'),'%')}，近15分钟 {s(it.get('vel_15m'),'%')}；"
        f"量能脉冲 {s(it.get('vol_pulse'),'×')}；日内位置 {s(it.get('range_pos'),'%')}(0=日低,100=日高)"
    )


def analyze_ticker(config, ticker, tech, news_text="", holding_pct=None,
                   tech_intraday=None, macro_text="", leverage=1.0):
    """返回 {stance, confidence, bull, bear, summary, levels, risk, tip}；失败时降级。
    tech=日线快照，tech_intraday=日内快照，macro_text=跨市场上下文，leverage=杠杆倍数。"""
    holding = f"（用户持有该标的，当前浮亏约 {holding_pct}%）" if holding_pct else ""
    lev = ""
    if leverage and float(leverage) > 1.0:
        lev = (f"（{leverage}x 杠杆 ETF：波动被放大 {leverage} 倍，且有每日复利损耗，"
               f"不适合长持、更要快进快出严控风险）")
    prompt = PROMPT_TMPL.format(
        ticker=ticker, leverage=lev, holding=holding,
        tech=_tech_text(tech), intraday=_intraday_text(tech_intraday),
        macro=(macro_text or "（暂无跨市场数据）"), news=(news_text or "（暂无相关新闻）"),
    )
    raw = ai_complete(config, prompt, system=SYSTEM, max_tokens=2000)
    sec = _parse_sections(raw)
    if sec.get("summary") or sec.get("stance"):
        return {
            "stance": (sec.get("stance") or "观望")[:12],
            "confidence": (sec.get("confidence") or "")[:6],
            "bull": sec.get("bull", ""),
            "bear": sec.get("bear", ""),
            "summary": sec.get("summary", ""),
            "levels": sec.get("levels", ""),
            "risk": sec.get("risk", ""),
            "tip": sec.get("tip", ""),
        }
    return {"stance": "观望", "confidence": "低", "bull": "", "bear": "",
            "summary": raw if not raw.startswith("[AI") else "", "levels": "", "risk": "",
            "tip": "", "error": raw if raw.startswith("[AI") else ""}


if __name__ == "__main__":
    import json, macro, market, news, signals
    cfg = json.load(open("config.json", encoding="utf-8")) if __import__("os").path.exists("config.json") else {}
    tk = "KORU"
    snap = signals.analyze(market.fetch_ohlc(tk))
    q = market.quote(tk)
    isnap = signals.analyze_intraday(market.fetch_intraday(tk), prev_close=q.get("prev_close") if q else None)
    res = analyze_ticker(cfg, tk, snap, news.news_digest(tk),
                         tech_intraday=isnap, macro_text=macro.context_text(cfg, focus=tk), leverage=3)
    for k, v in res.items():
        print(f"【{k}】{v}")
