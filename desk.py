"""
全天候盯盘循环（盘前/盘中/盘后分钟级 + 隔夜期货&亚洲代理）：
按美东时段切换节奏 → 扫描关注列表(日线+分钟) → 日内/日线事件触发(去重/冷却)
→ 关联新闻 + 跨市场宏观 → AI 多空辩论(吃日内+宏观+杠杆) → 存信号 → 推飞书。
隔夜个股/ETF 不交易：监控持有杠杆 ETF 的挂钩市场(如 KORU→KOSPI)，大动则发"开盘前预警"。
另含每日盘前预判 / 盘后复盘，与手动分析单只。跑在后台 daemon 线程。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import feishu
import llm
import macro
import market
import news
import signals

BJ = timezone(timedelta(hours=8))

# 常见杠杆 ETF 倍数（config.LEVERAGE_MAP 可覆盖）
DEFAULT_LEVERAGE = {
    "SOXL": 3, "SOXS": 3, "USD": 2, "TQQQ": 3, "SQQQ": 3, "SPXL": 3, "SPXS": 3, "UPRO": 3,
    "KORU": 3, "YINN": 3, "YANG": 3, "NVDL": 2, "NVDU": 2, "TSLL": 2, "AMDL": 2, "LABU": 3,
}

# 硬触发：即便信号标签没变也值得提醒
HARD_EVENTS = {
    "drop", "break_ma50", "new_low", "rsi_os", "rsi_ob", "macd_dead", "macd_golden",
    "fast_drop", "fast_pop", "gap", "vwap_break", "intraday_low", "preopen_risk",
    "t_buy", "t_sell",
}


def _now_bj():
    return datetime.now(BJ)


def _utc_iso():
    return datetime.now(timezone.utc).isoformat()


class Desk:
    def __init__(self, config, store, status):
        self.config = config
        self.store = store
        self.status = status
        self._stop = threading.Event()
        self._prev_daily = {}    # ticker -> 上一次日线快照
        self._prev_intra = {}    # ticker -> 上一次日内快照
        self._last_alert = {}    # ticker -> {"key":..., "ts": datetime}
        self._primed = False
        self._last_review = {}   # phase -> 北京日期
        self._last_check_bj = None   # 上一轮复盘检查的北京时间（跨越式判定用）

    def stop(self):
        self._stop.set()

    def reload_config(self, cfg):
        """运行中热替换配置（整体替换 dict 引用；读侧只读，无需锁）。"""
        self.config = cfg
        self._last_check_bj = None   # 复盘判定重置，避免跨配置漏判

    # ---- 关注列表（做完整信号+AI 的标的：大盘+个股+杠杆ETF+自选）----
    def _watchlist(self):
        groups = [self.config.get("DESK_INDICES") or [],
                  self.config.get("DESK_TICKERS") or [],
                  self.config.get("DESK_LEVERAGED") or [],
                  self.config.get("DESK_CUSTOM") or [],
                  self.config.get("DESK_MENTIONED") or []]
        seen, out = set(), []
        for g in groups:
            for t in g:
                t = (t or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    def _leverage_of(self, ticker):
        m = dict(DEFAULT_LEVERAGE)
        m.update(self.config.get("LEVERAGE_MAP") or {})
        try:
            return float(m.get(ticker, 1) or 1)
        except Exception:
            return 1.0

    def _prepost(self):
        return bool(self.config.get("DESK_PREPOST", True))

    # ---- 单只：算日线+日内信号 + 合并事件 ----
    def _scan_one(self, ticker, with_intraday=True):
        daily = signals.analyze(market.fetch_ohlc(ticker))
        events = []
        if daily:
            events += signals.detect_events(self._prev_daily.get(ticker), daily, self.config)
        intra = None
        if with_intraday:
            q = market.quote(ticker)
            prev_close = q.get("prev_close") if q else None
            intra = signals.analyze_intraday(
                market.fetch_intraday(ticker, interval="1m", prepost=self._prepost()), prev_close)
            if intra:
                events += signals.detect_intraday_events(
                    self._prev_intra.get(ticker), intra, self.config, leverage=self._leverage_of(ticker))
        return daily, intra, events

    @staticmethod
    def _alert_key(label, events):
        return (label or "") + "|" + ",".join(sorted(e["type"] for e in events))

    # ---- 是否该报警（去重 + 冷却）。只判定，不提交冷却（提交放到推送成功后）----
    def _should_alert(self, ticker, label, events):
        prev = self._prev_daily.get(ticker)
        label_changed = prev is not None and prev.get("label") != label
        has_hard = any(e["type"] in HARD_EVENTS for e in events)
        if not events and not label_changed:
            return False
        if not has_hard and not label_changed and all(e["level"] == "info" for e in events):
            return False
        last = self._last_alert.get(ticker)
        cooldown = float(self.config.get("DESK_COOLDOWN", 14400))
        if last and last["key"] == self._alert_key(label, events) \
                and (datetime.now(timezone.utc) - last["ts"]).total_seconds() < cooldown:
            return False
        return True

    # ---- 生成一条信号（含 AI 辩论）并存储 + 推送 ----
    def _emit(self, ticker, daily, intra, events, kind="signal", push=True, label_override=None):
        holding = (self.config.get("HOLDINGS") or {}).get(ticker)
        leverage = self._leverage_of(ticker)
        macro_text = macro.context_text(self.config, focus=ticker)
        ai = {}
        try:
            ai = llm.analyze_ticker(self.config, ticker, daily, news.news_digest(ticker),
                                    holding_pct=holding, tech_intraday=intra,
                                    macro_text=macro_text, leverage=leverage)
        except Exception as e:
            ai = {"stance": "观望", "summary": f"[AI失败] {e}", "error": str(e)}
        label = label_override or (daily or {}).get("label") or "观望-中性"
        sig = {
            "id": f"{ticker}-{int(time.time()*1000)}",
            "ticker": ticker,
            "ts": _utc_iso(),
            "ts_bj": _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            "session": market.SESSION_LABEL.get(market.session_now(), ""),
            "label": label,
            "leverage": leverage,
            "events": events,
            "tech": daily,
            "intraday": intra,
            "macro": macro_text,
            "ai": ai,
            "kind": kind,
        }
        self.store.add(sig)
        if push:
            title, content, template = feishu.build_desk_card(sig)
            results = feishu.push_all(feishu.bots_from_config(self.config), title, content,
                                      template=template, site_url=self.config.get("SITE_URL", ""))
            ok = any(o for _, o, _ in results)
            # 冷却只在"真的推出去了"之后才提交：推送失败不占用 4h 冷却，避免最该提醒时被静默吞掉
            if ok and kind in ("signal", "preopen"):
                self._last_alert[ticker] = {"key": self._alert_key(label, events),
                                            "ts": datetime.now(timezone.utc)}
            print(f"  飞书盯盘推送 {ticker} [{label}] → {[(n, o) for n, o, _ in results]}")
        return sig

    # ---- 手动分析单只（后台按钮调用）----
    def analyze_one(self, ticker, push=True):
        daily, intra, events = self._scan_one(ticker)
        if not daily and not intra:
            return None, "数据不足或拉取失败"
        if daily:
            self._prev_daily[ticker] = daily
        if intra:
            self._prev_intra[ticker] = intra
        sig = self._emit(ticker, daily, intra, events, kind="manual", push=push)
        return sig, "ok"

    # ---- 盘前/盘中/盘后：分钟级扫描 ----
    def _scan_intraday(self):
        for t in self._watchlist():
            if self._stop.is_set():
                break
            try:
                daily, intra, events = self._scan_one(t)
            except Exception as e:
                print(f"  扫描 {t} 出错：{e}")
                continue
            if not daily and not intra:
                continue
            label = (daily or {}).get("label", "观望-中性")
            if self._primed and self._should_alert(t, label, events):
                try:
                    self._emit(t, daily, intra, events, kind="signal", push=True)
                    self.status["alerts_sent"] = self.status.get("alerts_sent", 0) + 1
                except Exception as e:
                    print(f"  推送 {t} 出错：{e}")
            if daily:
                self._prev_daily[t] = daily
            if intra:
                self._prev_intra[t] = intra
            self.status["scanned"] = self.status.get("scanned", 0) + 1

    # ---- 隔夜：个股/ETF 不交易，盯持有杠杆 ETF 的挂钩市场，大动发"开盘前预警" ----
    def _scan_overnight(self):
        th = float(self.config.get("DESK_OVERNIGHT_ALERT", 2.5))
        leveraged = self.config.get("DESK_LEVERAGED") or []
        for t in leveraged:
            if self._stop.is_set():
                break
            und = macro.underlying_of(self.config, t)
            if not und:
                continue
            q = market.quote(und)
            if not q:
                continue
            chg = q.get("change_pct") or 0
            if abs(chg) < th:
                continue
            level = "danger" if chg < 0 else "warn"
            uname = macro.NAMES.get(und, und)
            ev = [{"type": "preopen_risk", "level": level,
                   "text": f"隔夜{uname} {chg:+.1f}% → {t} 开盘前预警（{self._leverage_of(t)}x 放大）"}]
            if not self._should_alert(t, "开盘前预警", ev):
                continue
            daily = signals.analyze(market.fetch_ohlc(t))
            try:
                self._emit(t, daily, None, ev, kind="preopen", push=True, label_override="开盘前预警")
                self.status["alerts_sent"] = self.status.get("alerts_sent", 0) + 1
            except Exception as e:
                print(f"  隔夜预警 {t} 出错：{e}")

    # ---- 每日复盘（盘前预判 / 盘后复盘，北京时间触发）----
    def _maybe_review(self):
        """跨越式判定：若复盘时刻落在 (上次检查, 现在] 之间则触发——无论扫描间隔多大、
        耗时多久都不会因窗口太窄而漏（旧的固定 600s 窗口==隔夜轮询间隔，易被刺穿）。"""
        cfg = self.config
        now = _now_bj()
        today = now.strftime("%Y-%m-%d")
        last = self._last_check_bj
        self._last_check_bj = now
        if last is None:
            return   # 首轮只记录基准，不触发（避免重启即补推）
        for phase, hhmm in [("盘前预判", cfg.get("DESK_REVIEW_PREMARKET", "21:00")),
                            ("盘后复盘", cfg.get("DESK_REVIEW_POSTMARKET", "05:00"))]:
            try:
                hh, mm = [int(x) for x in str(hhmm).split(":")]
            except Exception:
                continue
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if last < target <= now and self._last_review.get(phase) != today:
                self._last_review[phase] = today
                try:
                    self._push_review(phase)
                except Exception as e:
                    print("  复盘出错：", e)

    def _push_review(self, phase):
        items = [(t, self._prev_daily.get(t) or signals.analyze(market.fetch_ohlc(t)))
                 for t in self._watchlist()]
        title, content, template = feishu.build_review_card(
            phase, items, self.config.get("SITE_URL", ""),
            macro_text=macro.context_text(self.config))
        feishu.push_all(feishu.bots_from_config(self.config), title, content,
                        template=template, site_url=self.config.get("SITE_URL", ""))
        print(f"  飞书 {phase} 已推送（{len(items)} 只）")

    # ---- 主循环（按时段切换节奏）----
    def run(self):
        self.status.update(running=True, current_status="正在初始化…", scanned=0, alerts_sent=0)
        print(f"开始全天候盯盘 {self._watchlist()}")

        while not self._stop.is_set():
            # 每轮重读节奏，使 reload_config / 设置变更即时生效
            intraday_iv = max(60, int(self.config.get("DESK_INTERVAL_INTRADAY", 180)))
            overnight_iv = max(120, int(self.config.get("DESK_INTERVAL_OVERNIGHT", 600)))
            sess = market.session_now()
            self.status["session"] = sess
            interval = intraday_iv
            try:
                if sess in ("premarket", "regular", "afterhours"):
                    self.status["current_status"] = f"{market.SESSION_LABEL[sess]}·分钟级扫描中…"
                    self._scan_intraday()
                    self._primed = True
                elif sess == "overnight":
                    self.status["current_status"] = "隔夜·盯期货/亚洲盘（杠杆ETF开盘前预警）"
                    interval = overnight_iv
                    self._scan_overnight()
                    self._primed = True
                else:  # closed / weekend
                    self.status["current_status"] = "休市·待命（仅定时复盘）"
                    interval = overnight_iv
            except Exception as e:
                print("  扫描循环出错：", e)
            self._maybe_review()
            self.status["last_scan"] = _utc_iso()
            self.status["next_scan"] = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
            self._stop.wait(interval)

        self.status.update(running=False, current_status="已停止")
        print("盯盘已停止。")
