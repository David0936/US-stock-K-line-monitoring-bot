# 使用教学 · Stock Desk 美股全天候盯盘

从零到跑起来，10 分钟。遇到问题欢迎提 [Issue](https://github.com/David0936/US-stock-K-line-monitoring-bot/issues)。

---

## 0. 准备
- Python 3.9+（macOS/Linux/WSL 均可）
- 一个能访问 Yahoo Finance 的网络（行情走免费 yfinance）
- 可选：一个 AI key（通义千问 / OpenAI / 中转站，任一 OpenAI 兼容即可）做「AI 多空辩论」
- 可选：一个飞书自定义机器人 webhook，做手机推送

## 1. 安装与启动
```bash
git clone https://github.com/David0936/US-stock-K-line-monitoring-bot.git
cd US-stock-K-line-monitoring-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json        # 可不改，启动后在网页里配
.venv/bin/python start.py                  # 打开 http://localhost:5005
```
- 控制台会打印**首次登录密码**（也写在 `data/default_password.txt`）。
- 换端口：`PORT=8000 .venv/bin/python start.py`。

## 2. 登录后台
打开 `http://localhost:5005`，右上角 **管理员登录**，输入上一步的密码。

![登录与盯盘台](screenshots/board.png)

## 3. 配置你自己的股票（核心）
进入 **设置**，按需填写后 **保存**：

| 项 | 怎么填 | 例子 |
|---|---|---|
| 大盘指数 | 空格/逗号分隔 | `^GSPC ^IXIC QQQ` |
| 个股 | 你关注的票 | `NVDA AMD TSM AVGO MU SMCI` |
| 杠杆 ETF | 你持有的杠杆票 | `SOXL SOXS USD KORU` |
| 指数期货 / 亚洲股市 | 隔夜方向代理 | `ES=F NQ=F` / `^KS11 ^N225 ^HSI 000001.SS` |
| 杠杆挂钩映射 | 每行 `ETF=挂钩代码`，隔夜预警用 | `KORU=^KS11`、`SOXL=^SOX` |
| 我的持仓 | 每行 `代码 浮亏%`，AI 针对性提醒 | `KORU 35`、`SOXL 20` |
| 阈值/节奏 | 急跌%、日内急跌%、隔夜异动%、扫描间隔、复盘时间 | 默认即可，按需调 |

![设置页](screenshots/settings.png)

> 代码怎么写：美股直接用代码（`NVDA`）；指数前面加 `^`（`^GSPC`=标普）；期货带 `=F`（`ES=F`）；A股/港股带后缀（`000001.SS`=上证、`0700.HK`=腾讯）。

## 4. 配置 AI（可选但强烈推荐）
设置页 **AI 设置**：
- `供应商` 选 **openai 兼容**：`Base URL` 填通义 `https://dashscope.aliyuncs.com/compatible-mode/v1`（默认）或官方 OpenAI 或你的中转站；`Model` 填 `qwen-plus` 等；`API Key` 填你的 key。
- 或选 **claude**：填 Anthropic 官方 key。
- AI 只在信号触发时调用，成本可控。

## 5. 配置飞书推送（可选）
1. 飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 webhook。
2. 设置页 **飞书机器人** 粘 webhook（多群可加多行），保存。
3. 关键信号（逃顶/抄底/急跌/破位/开盘前预警）会自动推过来，外加每日盘前/盘后复盘。

## 6. 启动盯盘
回盯盘台，右上角点 **▶ 启动**。状态条会显示当前时段（盘前/盘中/盘后/隔夜）与扫描节奏。
- 改了设置后再保存，会**即时热生效**，无需重启。

## 7. 看懂盯盘台
- **顶部状态条**：盯盘状态、当前时段、北京/美东双时钟、扫描节奏。
- **宏观条**：期货 + 亚洲盘，涨红跌绿——隔夜个股不动时看这里判方向。
- **分组看板**：大盘 / 期货 / 亚洲 / 个股 / 杠杆ETF，每张卡片有现价、涨跌、mini 走势线、信号标签。点卡片进**个股详情**。
- **AI 信号流**：每条 = 标的 + 信号标签 + AI 倾向（买/卖/逃顶/抄底/观望）+ 综合判断 + 关键位。

## 8. 个股详情页
![个股详情 + K线](screenshots/detail.png)
- **分钟 K 线图**（lightweight-charts），可切 1m/5m/15m/1D。
- **信号史**：历次信号 + 多空理由 + 关键位 + 风险 + 新手提醒。
- **相关新闻**：自动关联该标的的 Yahoo 新闻。
- 右上 **🤖 立即 AI 分析**：手动跑一次（也会推飞书，可用来测试推送链路）。

## 9. 飞书卡片长这样
![飞书卡片](screenshots/feishu.png)

## 信号标签速查
| 标签 | 含义 | 颜色 |
|---|---|---|
| 逃顶预警 | 超买 + 乖离过大，谨防追高 | 红 |
| 减仓-趋势破位 | 跌破关键均线/空头排列 | 红 |
| 开盘前预警 | 隔夜挂钩市场大动（杠杆ETF） | 红 |
| 抄底区-超卖 | 超卖/深跌，逆周期机会 | 绿 |
| 持有-趋势健康 | 多头排列、站稳均线 | 蓝 |
| 观望-中性 | 信号不明，等方向 | 黑 |

## 常见问题 FAQ
- **报价/分钟线偶尔为空？** yfinance 偶发限流或网络波动；项目已加缓存+负缓存+按时段降频，稍等即恢复。指数（`^GSPC`）无盘前分钟线属正常。
- **隔夜个股卡片不动？** 美股个股隔夜（美东 20:00–次日 04:00）不交易，看宏观条的期货/亚洲盘即可。
- **AI 没输出？** 检查设置页 AI key / Base URL / Model 是否正确，供应商选对（中转站走 openai 兼容，不要选 claude）。
- **想换更稳的数据源？** 数据源封装在 `market.py`，可平滑换 Finnhub / Stooq。

---
⚠️ 所有信号与 AI 判断仅为信息提示，非投资建议。杠杆 ETF 高波动 + 复利损耗，务必严格风控、盈亏自负。
