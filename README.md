# 📈 Stock Desk · 美股全天候盯盘终端

> 把 **K线技术信号** + **当下新闻** + **跨市场宏观（期货/亚洲盘）** 联动起来，用 **AI 多空辩论**
> 给出「买入 / 持有 / 观望 / 减仓 / 逃顶 / 抄底」的明确倾向 + 关键价位 + 风险 + 新手提醒，
> **关键信号才推飞书**，专治新手「追涨杀跌、总做反、新闻和股价对不上」。

一个**自托管、免费数据源（yfinance）**的美股盯盘机器人。盯你自己的股票，全天候（盘前 / 盘中 /
盘后 / 隔夜）分钟级监控，关键信号推送到飞书。**社区版完全开源**；需要托管部署、自定义策略、
更多数据源/推送渠道等高级能力，见文末「高级版」联系方式。

UI 采用 [huashu-design](https://github.com/alchaincyf/huashu-design) 的「FT × 彭博终端」编辑风：
浅底报纸感 + 1px 规则线网格 + 等宽字排数字 + 涨红跌绿，零花哨。

---

## ✨ 功能

- **全天候按时段调度**（美东时区，自动处理夏/冬令时与节假日）
  - 盘前 / 盘中 / 盘后：个股、ETF **分钟级**扫描（含盘前盘后 04:00–20:00 ET）
  - **隔夜**：个股不交易 → 盯**指数期货 + 亚洲股市**做方向代理，持有的杠杆 ETF 挂钩市场大动时发**「开盘前预警」**
- **技术信号引擎**：日线 RSI / MACD / 均线(20/50/200) / 布林 / ATR / 量比 / 回撤；日内 VWAP / 日内RSI / 涨速 / 量能脉冲；事件检测（急跌急涨、破位、超买超卖、金死叉、新高新低、跳空、放量脉冲）
- **跨市场联动**：期货(ES=F/NQ=F) + 亚洲(KOSPI/日经/恒指/上证)，杠杆 ETF 自动挂钩标的市场（KORU→韩国、SOXL→费城半导体…）
- **AI 多空辩论**：把技术面 + 日内 + 宏观 + 新闻喂给大模型，分别陈述多头/空头最强论据，再给客观结论、关键位、风险与「别追高别割肉」新手提醒；对杠杆 ETF 强调放大与复利损耗
- **资讯**：Yahoo Finance 新闻自动关联到对应标的
- **飞书推送**：interactive 卡片，关键信号才推（冷却去重防刷屏）+ 每日盘前预判 / 盘后复盘
- **Web 盯盘台**：分组看板（大盘/期货/亚洲/个股/杠杆ETF）+ 实时报价 + 宏观条 + AI 信号流 + 个股**分钟 K 线图** + mini 走势线
- **你自己的股票**：关注列表、杠杆倍数、挂钩映射、持仓浮亏、各类阈值与复盘时间，全部在 `/settings` 里自定义

## 🚀 快速开始

```bash
git clone <this-repo>
cd stock-desk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.json config.json   # 然后按需编辑（也可不改，启动后在 /settings 里配）
.venv/bin/python start.py            # 打开 http://localhost:5005
```

- 首次登录密码见控制台启动日志，或 `data/default_password.txt`。
- 登录后到 **设置** 填：你的关注标的 / 持仓浮亏 / 飞书 webhook / AI key / 阈值与复盘时间，保存后回盯盘台点 **▶ 启动**。
- 改端口：`PORT=8000 python start.py`。

### 模块自测
```bash
.venv/bin/python market.py    # 行情 + 时段 + 分钟线
.venv/bin/python signals.py   # 日线 + 日内信号
.venv/bin/python macro.py     # 跨市场宏观上下文
.venv/bin/python llm.py       # AI 多空辩论（需配好 AI key）
```

## ⚙️ 配置要点

| 配置 | 说明 |
|---|---|
| `DESK_INDICES / DESK_TICKERS / DESK_LEVERAGED` | 你要盯的大盘 / 个股 / 杠杆 ETF（在 /settings 里填代码即可）|
| `DESK_FUTURES / DESK_ASIA` | 隔夜方向代理：指数期货 / 亚洲股市 |
| `UNDERLYING_MAP` | 杠杆 ETF 挂钩市场（如 `KORU=^KS11`），用于隔夜「开盘前预警」 |
| `LEVERAGE_MAP` | 杠杆倍数（内置常见 ETF，可覆盖）|
| `HOLDINGS` | 你的持仓与浮亏（`代码 浮亏%`），AI 会针对性提醒 |
| `AI_PROVIDER / LLM_*` / `CLAUDE_*` | AI 供应商：OpenAI 兼容（通义/官方/中转站）或 Claude 官方 |

## 🔌 AI 供应商

`AI_PROVIDER=openai` 走 OpenAI 兼容接口，`LLM_URL` 可填官方 OpenAI、通义千问
（`https://dashscope.aliyuncs.com/compatible-mode/v1`，默认）或任意兼容中转站；
`AI_PROVIDER=claude` 走 Anthropic 官方。AI 仅在信号触发时调用，成本可控。

## 📊 数据源说明

行情走 [yfinance](https://github.com/ranaroussi/yfinance)（免费，无需 key）。注意：
- 个股/ETF 分钟线含盘前盘后（美东 04:00–20:00）；**隔夜 20:00–04:00 个股不交易**，用期货+亚洲盘代理方向。
- yfinance 偶发限流、需稳定外网；项目已做缓存 + 负缓存 + 按时段降频。数据源封装在 `market.py`，可平滑换 Finnhub / Stooq 等。

## ⚠️ 免责声明

本项目仅为信息聚合与技术分析工具。所有行情、信号、AI 倾向与关键位均为**信息提示，
不构成任何投资建议**。杠杆 ETF 含每日再平衡衰减与高波动风险。请独立判断、严格风控、
盈亏自负。作者不对任何交易决策与损失负责。

## 🧱 社区版 vs 高级版

**社区版（本仓库，MIT 开源）**：上述全部功能，自托管、自配数据源与推送，盯你自己的股票。

**高级版 / 定制（联系作者）**：托管部署免运维、更稳的付费数据源、多渠道推送（企业微信/Telegram/邮件）、
自定义策略与回测、组合级风控、多用户等。

📮 **联系作者 / 高级版咨询**

- 微信公众号：**自家的鱼鱼 / Claworld**
- X (Twitter)：[@Shark1996_](https://x.com/shark1996_)
- YouTube：[@Singularity2026](https://www.youtube.com/@Singularity2026)
- 小红书：[David小鱼](https://xhslink.com/m/6WBQosGc8F6)
- GitHub：[@David0936](https://github.com/David0936) · 欢迎提 [Issue](https://github.com/David0936/US-stock-K-line-monitoring-bot/issues)

## 🙏 致谢

- [yfinance](https://github.com/ranaroussi/yfinance) · 免费行情
- [TradingView lightweight-charts](https://github.com/tradingview/lightweight-charts) · K 线图
- [huashu-design](https://github.com/alchaincyf/huashu-design) · UI 设计语言
- 由 [Claude Code](https://claude.com/claude-code) 协助开发

## 📄 License

[MIT](LICENSE) © 2026 David0936
