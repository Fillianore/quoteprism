# -*- coding: utf-8 -*-
"""股票监控 —— 核心逻辑（行情抓取 / 搜索 / 提醒引擎）

零第三方依赖，只用标准库。
行情来源（自动降级）：
  1) 东方财富 push2      —— 支持沪深京A股/指数/基金、港股、美股
  2) 新浪 hq.sinajs.cn   —— 兜底，仅沪深京
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_NAME = "股票监控"
APP_VERSION = "1.7"

# --------------------------------------------------------------------------
# 长期运行基础设施：磁盘缓存 + 限速 + 退避（见 stock_cache.py）
# --------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from stock_cache import DiskCache, RateLimiter, jittered  # noqa: F401
except Exception:  # pragma: no cover - 极端情况下退化为不缓存
    class DiskCache:  # type: ignore[no-redef]
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return None
        def get_stale(self, *a, **k): return None
        def put(self, *a, **k): pass

    class RateLimiter:  # type: ignore[no-redef]
        def __init__(self, *a, **k): pass
        def allow(self, *a, **k): return True
        def wait_seconds(self, *a, **k): return 0.0
        def success(self, *a, **k): pass
        def failure(self, *a, **k): return 0.0
        def stats(self): return {}

    def jittered(interval: float, ratio: float = 0.15) -> float:  # type: ignore[misc]
        return interval

def _base_dir() -> str:
    """数据目录（stocks.json / cache / 日志）。

    `--config <目录>` 可覆盖，便于把程序装在只读位置（如插件包内）而把数据写到别处。
    """
    argv = sys.argv
    for idx, arg in enumerate(argv):
        cand = ""
        if arg in ("--config", "-c") and idx + 1 < len(argv):
            cand = argv[idx + 1]
        elif arg.startswith("--config="):
            cand = arg.split("=", 1)[1]
        if cand:
            cand = os.path.abspath(os.path.expanduser(cand))
            try:
                os.makedirs(cand, exist_ok=True)
                return cand
            except Exception:
                break
    return os.path.dirname(os.path.abspath(__file__))


_BASE_DIR = _base_dir()
CACHE = DiskCache(_BASE_DIR)
LIMITER = RateLimiter(min_interval=0.6, max_backoff=600.0)
# 各类数据的缓存时长（秒）：日线一天基本不变，分时/资金流当日累计、短TTL
CACHE_TTL = {
    "kline_daily": 900,      # 日/周/月线：15 分钟
    "kline_minute": 45,      # 分钟线：45 秒
    "tick": 30,              # 当日分时：30 秒
    "flow_history": 1800,    # 历史资金流：30 分钟（日级数据）
    "flow_intraday": 30,     # 当日资金流分时：30 秒
}

# --------------------------------------------------------------------------
# 磁盘配置
# --------------------------------------------------------------------------
DEFAULT_STOCKS: List[Dict[str, str]] = [
    {"secid": "1.600519", "code": "600519", "name": "贵州茅台"},
    {"secid": "0.300750", "code": "300750", "name": "宁德时代"},
    {"secid": "0.000001", "code": "000001", "name": "平安银行"},
    {"secid": "1.601899", "code": "601899", "name": "紫金矿业"},
    {"secid": "1.600036", "code": "600036", "name": "招商银行"},
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "stocks": DEFAULT_STOCKS,
    "refresh_sec": 3,          # 交易时段刷新间隔（秒）
    "idle_refresh_sec": 60,    # 非交易时段刷新间隔（秒）
    "control_port": 8765,      # 本地控制端口（供外部程序唤起窗口）
    "tushare_token": "",       # Tushare Pro token（留空=不启用）
    "tushare_enabled": True,   # 有 token 时是否优先用 Tushare
    "always_on_top": True,
    "collapsed": True,
    "selected": "1.600519",    # 收起状态下悬浮球显示的股票
    "sort": {"col": "default", "desc": False},
    "alerts": {},              # {secid: {"above": float, "below": float, "pct": float, "on": bool}}
    "panel_geometry": "",
    "ball_pos": None,
    "detail_geometry": "",
    "detail_period": "daily",
    "detail_adjust": 1,
    "detail_view_bars": 90,
    "hint_shown": False,
    "auto_height": True,
    "notify_mode": "popup",    # popup | system | both
    "sound": True,
    "paused": False,
    # ---- 悬浮条轮播（首页小球滚动显示自选）----
    "ball_rotate_sec": 6,      # 每 N 秒切下一只；0=关闭轮播
    # ---- 分时曲线平滑（保形插值：经过真实点、不造新高低点）----
    "tick_smooth": True,       # False = 显示原始折线（每 1 分钟一段直线）
    # ---- 全球通货（贵金属 / 原油 / 外汇）----
    "show_commodities": True,
    "commodities": [
        {"code": "hf_GC", "name": "纽约黄金", "unit": "美元/盎司"},
        {"code": "hf_SI", "name": "纽约白银", "unit": "美元/盎司"},
        {"code": "hf_CL", "name": "纽约原油", "unit": "美元/桶"},
        {"code": "hf_OIL", "name": "布伦特原油", "unit": "美元/桶"},
        {"code": "nf_AU0", "name": "沪金主力", "unit": "元/克"},
        {"code": "nf_AG0", "name": "沪银主力", "unit": "元/千克"},
        {"code": "nf_SC0", "name": "上海原油", "unit": "元/桶"},
        {"code": "fx_susdcny", "name": "美元人民币", "unit": ""},
        {"code": "100.UDI", "name": "美元指数", "unit": ""},
    ],
}


class Config:
    """极简 JSON 配置存储。"""

    def __init__(self, path):
        self.path = str(path)
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for key, val in raw.items():
                    self.data[key] = val
        except FileNotFoundError:
            self.save()
        except Exception:
            # 配置损坏时不覆盖用户文件，仅在内存里用默认值
            pass

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            # 保护性合并：内存里 token 为空但磁盘上有值时，保留磁盘上的。
            # （旧版本实例内存里没有 token，一保存就会把用户填的 token 清掉。）
            if not str(self.data.get("tushare_token") or "").strip():
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        on_disk = str(json.load(fh).get("tushare_token") or "").strip()
                    if on_disk:
                        self.data["tushare_token"] = on_disk
                except Exception:
                    pass
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- 自选股 -----------------------------------------------------------
    def stocks(self) -> List[Dict[str, str]]:
        out = []
        for item in self.data.get("stocks") or []:
            if isinstance(item, dict) and item.get("secid"):
                out.append({
                    "secid": str(item["secid"]),
                    "code": str(item.get("code", "")),
                    "name": str(item.get("name", "")),
                })
            elif isinstance(item, str):  # 容错：允许写成字符串
                parsed = parse_symbol(item)
                if parsed:
                    out.append(parsed)
        return out

    def set_stocks(self, stocks: List[Dict[str, str]]) -> None:
        self.data["stocks"] = [dict(s) for s in stocks]

    def add_stock(self, item: Dict[str, str]) -> bool:
        stocks = self.stocks()
        if any(s["secid"] == item["secid"] for s in stocks):
            return False
        stocks.append({"secid": item["secid"], "code": item["code"], "name": item["name"]})
        self.set_stocks(stocks)
        return True

    def remove_stock(self, secid: str) -> None:
        self.set_stocks([s for s in self.stocks() if s["secid"] != secid])
        if self.data.get("selected") == secid:
            rest = self.stocks()
            self.data["selected"] = rest[0]["secid"] if rest else ""
        self.data.get("alerts", {}).pop(secid, None)

    def move_stock(self, secid: str, delta: int) -> None:
        stocks = self.stocks()
        idx = next((i for i, s in enumerate(stocks) if s["secid"] == secid), -1)
        if idx < 0:
            return
        new = max(0, min(len(stocks) - 1, idx + delta))
        if new != idx:
            stocks.insert(new, stocks.pop(idx))
            self.set_stocks(stocks)

    # ---- 提醒 -------------------------------------------------------------
    def alerts(self) -> Dict[str, Any]:
        al = self.data.get("alerts")
        if not isinstance(al, dict):
            al = {}
            self.data["alerts"] = al
        return al

    def alert_for(self, secid: str) -> Dict[str, Any]:
        rule = self.alerts().get(secid)
        if not isinstance(rule, dict):
            rule = {}
        rule.setdefault("above", None)
        rule.setdefault("below", None)
        rule.setdefault("pct", None)
        rule.setdefault("on", True)
        rule.setdefault("_armed_above", True)
        rule.setdefault("_armed_below", True)
        rule.setdefault("_armed_pct", True)
        self.alerts()[secid] = rule
        return rule

    def has_alert(self, secid: str) -> bool:
        rule = self.alerts().get(secid) or {}
        return bool(rule.get("above") or rule.get("below") or rule.get("pct"))


# --------------------------------------------------------------------------
# 代码解析 / 市场判断
# --------------------------------------------------------------------------
_HK_RE = re.compile(r"^(\d{4,5})$")
_DIGITS_RE = re.compile(r"^(\d{6})$")


def parse_symbol(text: str) -> Optional[Dict[str, str]]:
    """把用户输入变成 {'secid','code','name'}，无法识别返回 None。

    支持：600519 / sh600519 / 600519.SH / hk00700 / 00700.HK / usAAPL / gb_aapl
    """
    raw = (text or "").strip().replace(" ", "")
    if not raw:
        return None
    low = raw.lower()

    # 前缀形式
    m = re.match(r"^(sh|sz|bj|hk|us|gb_)(.+)$", low)
    if m:
        prefix, body = m.group(1), m.group(2)
        if prefix in ("sh", "sz", "bj") and body.isdigit():
            return {"secid": f"{market_of(body)}.{body}", "code": body, "name": ""}
        if prefix == "hk" and body.isdigit():
            return {"secid": f"116.{body.zfill(5)}", "code": body.zfill(5), "name": ""}
        if prefix in ("us", "gb_") and body:
            return {"secid": f"105.{body.upper()}", "code": body.upper(), "name": ""}

    # 后缀形式
    m = re.match(r"^(\d{6})\.(sh|sz|bj)$", low)
    if m:
        return {"secid": f"{market_of(m.group(1))}.{m.group(1)}", "code": m.group(1), "name": ""}
    m = re.match(r"^(\d{4,5})\.hk$", low)
    if m:
        return {"secid": f"116.{m.group(1).zfill(5)}", "code": m.group(1).zfill(5), "name": ""}

    # 纯数字：6 位=A股指数/股票，5 位=港股，4 位=港股
    m = _DIGITS_RE.match(raw)
    if m:
        code = m.group(1)
        return {"secid": f"{market_of(code)}.{code}", "code": code, "name": ""}
    m = _HK_RE.match(raw)
    if m:
        code = m.group(1).zfill(5)
        return {"secid": f"116.{code}", "code": code, "name": ""}

    # 纯字母：美股
    if re.match(r"^[A-Za-z][A-Za-z.\-]{0,9}$", raw):
        code = raw.upper()
        return {"secid": f"105.{code}", "code": code, "name": ""}
    return None


def market_of(code: str) -> int:
    """6 位 A 股代码 -> 市场号（0=深, 1=沪, 0/1 兼容北交所）。"""
    c = code.strip()
    if c.startswith(("5", "6", "9", "11", "13", "18", "50", "51", "52", "56", "58")):
        return 1
    return 0


def market_label(secid: str) -> str:
    mk = secid.split(".")[0]
    return {"0": "深", "1": "沪", "105": "美", "106": "美", "107": "美",
            "116": "港", "128": "港"}.get(mk, "")


# --------------------------------------------------------------------------
# 行情数据结构
# --------------------------------------------------------------------------
@dataclass
class Commodity:
    """贵金属 / 原油 / 外汇等"通货"行情。

    与 A 股股票分开建模：它们没有资金流、没有复权、涨跌幅口径也不同，
    混进 Quote 会让下游逻辑到处特判。
    """
    code: str                 # 内部键，如 hf_GC / nf_AU0 / fx_susdcny / 100.UDI
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    pct: float = 0.0
    unit: str = ""            # 单位提示（美元/盎司、元/克…）
    quote_time: str = ""
    source: str = ""
    stale: bool = False       # True=本次没取到，用的是上次成功值
    ts: float = field(default_factory=time.time)


@dataclass
class Quote:
    secid: str
    code: str = ""
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0        # 手
    amount: float = 0.0        # 元
    volume_ratio: float = 0.0  # 量比
    turnover: float = 0.0      # 换手率 %
    pe: float = 0.0
    cap: float = 0.0           # 总市值
    pct_hint: Optional[float] = None      # 接口直接给的涨跌幅（兜底；None=接口没给）
    change_hint: Optional[float] = None   # 接口直接给的涨跌额（兜底）
    # ---- 资金流（当日，来自东财 f62 系列）----
    main_net: float = 0.0      # 主力净流入（元）
    main_net_pct: float = 0.0  # 主力净占比 %
    super_net: float = 0.0     # 超大单净额
    super_net_pct: float = 0.0
    big_net: float = 0.0       # 大单净额
    big_net_pct: float = 0.0
    mid_net: float = 0.0       # 中单净额
    mid_net_pct: float = 0.0
    small_net: float = 0.0     # 小单净额
    small_net_pct: float = 0.0
    quote_time: str = ""       # 行情自带时间 HH:MM:SS（用于识别是否还在成交）
    date_hint: str = ""        # 行情自带日期
    ts: float = field(default_factory=time.time)

    @property
    def quote_age_sec(self) -> Optional[float]:
        """行情时间戳距现在多少秒（用于判断数据是否是「活的」）。"""
        if not self.quote_time:
            return None
        try:
            parts = self.quote_time.split(":")
            now = datetime.now()
            stamp = now.replace(hour=int(parts[0]), minute=int(parts[1]),
                                second=int(parts[2]) if len(parts) > 2 else 0, microsecond=0)
            return abs((now - stamp).total_seconds())
        except Exception:
            return None

    @property
    def is_live(self) -> bool:
        """数据是否还在跳动（休市时接口会一直返回最后成交时间）。"""
        age = self.quote_age_sec
        if age is None:
            return is_trading_time()
        return age <= 90

    @property
    def change(self) -> float:
        if self.price and self.prev_close:
            return self.price - self.prev_close
        return self.change_hint if self.change_hint is not None else 0.0

    @property
    def pct(self) -> float:
        if self.price and self.prev_close:
            return (self.price - self.prev_close) / self.prev_close * 100.0
        return self.pct_hint if self.pct_hint is not None else 0.0

    @property
    def is_stale(self) -> bool:
        """价格来自昨收且不在交易时段 —— 视为停牌/未开盘。"""
        return self.price == 0.0

    @property
    def display_name(self) -> str:
        return self.name or self.code or self.secid


# --------------------------------------------------------------------------
# 行情客户端
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EM_QUOTE_FIELDS = ("f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f8,f9,f10,f20,f21,"
                   "f62,f184,f66,f69,f72,f75,f78,f81,f84,f87")
EM_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_KLINE_MIRROR = "https://push2delay.eastmoney.com/api/qt/stock/kline/get"
EM_FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 当日资金流分时（klt=1）：每分钟累计净流入，用于"当日主力"走势
EM_FFLOW_KLINE_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "CN_MarketData.getKLineData")
# 1 分钟专用端点：CN_MarketData 的 scale=1 返回 null，
# 且 money.finance 主机对该接口返回 "Service not valid"，必须用 quotes.sina.cn。
SINA_MIN1_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/"
                 "CN_MarketDataService.getKLineData")
SINA_URL = "https://hq.sinajs.cn/list="
UT_TOKEN = "bd1d9ddb04089700cf9c27f6f7426281"


def _fnum(val: Any, default: float = 0.0) -> float:
    try:
        if val in ("-", "", None):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 简易熔断：按"主机 + 路径"粒度冷却，避免一个接口被限流连坐整台主机
#
# 早期版本按主机名熔断，结果 /stock/kline 正常、/stock/fflow 被拒时，
# 整台 push2his 被冷却，日K跟着一起挂掉。
# --------------------------------------------------------------------------
_HOST_COOLDOWN: Dict[str, float] = {}
# 冷却时长：公开接口被拒后短时间内反复重试只会让封禁更久，设长一点更稳
COOLDOWN_SEC = 240.0


def _cooldown_key(url: str) -> str:
    """主机 + 具体接口名（/api/qt/ulist.np、/api/qt/stock/kline 等）。

    粒度必须到接口级：push2his 上 kline 正常而 fflow 被拒是常见情况。
    """
    try:
        parts = urllib.parse.urlparse(url)
        segs = [s for s in parts.path.split("/") if s]
        resource = ""
        if "stock" in segs:
            idx = segs.index("stock")
            if idx + 1 < len(segs):
                resource = segs[idx + 1]
        elif segs:
            resource = segs[-1]
        if len(segs) > 3 and segs[0] == "api" and segs[1] == "qt" and not resource:
            resource = segs[2]
        return f"{parts.netloc}/{resource}" if resource else parts.netloc
    except Exception:
        return url


def _host_blocked(url: str) -> bool:
    return time.time() < _HOST_COOLDOWN.get(_cooldown_key(url), 0.0)


def _mark_blocked(url: str, seconds: float = COOLDOWN_SEC) -> None:
    try:
        key = _cooldown_key(url)
        _HOST_COOLDOWN[key] = time.time() + seconds
        LIMITER.failure(key)          # 同步拉长限速窗口，避免冷却结束后立刻又猛敲
        log_line = f"[{datetime.now():%H:%M:%S}] 数据源暂时不可用，冷却 {seconds:.0f}s: {key}"
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "stock_monitor.log"), "a", encoding="utf-8") as fh:
                fh.write(log_line + "\n")
        except Exception:
            pass
    except Exception:
        pass


def _http_get(url: str, referer: str = "", timeout: float = 8.0, retries: int = 1) -> bytes:
    """带一次重试的 GET；连接被重置（限流）时标记该主机冷却并抛出。"""
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        if _host_blocked(url):
            raise RuntimeError("数据源冷却中")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": referer or "https://quote.eastmoney.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError,) as exc:
            last_exc = exc
            if exc.code in (403, 418, 429):
                _mark_blocked(url)
                break
        except Exception as exc:  # noqa: BLE001  (含 RemoteDisconnected / 超时)
            last_exc = exc
            name = type(exc).__name__
            if name in ("RemoteDisconnected", "ConnectionResetError", "BadStatusLine") or \
                    "Connection reset" in str(exc):
                _mark_blocked(url)
                break
            if attempt >= retries:
                break
            time.sleep(0.6 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("请求失败")


def fetch_quotes_eastmoney(secids: List[str], timeout: float = 8.0) -> Dict[str, Quote]:
    if not secids:
        return {}
    params = {
        "secids": ",".join(secids),
        "fields": EM_QUOTE_FIELDS,
        "fltt": "2",
        "invt": "2",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "_": str(int(time.time() * 1000)),
    }
    url = EM_QUOTE_URL + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=timeout).decode("utf-8", "replace"))
    diff = ((payload or {}).get("data") or {}).get("diff") or []
    out: Dict[str, Quote] = {}
    for row in diff:
        code = str(row.get("f12", ""))
        mk = str(row.get("f13", ""))
        secid = f"{mk}.{code}"
        out[secid] = Quote(
            secid=secid,
            code=code,
            name=str(row.get("f14", "") or ""),
            price=_fnum(row.get("f2")),
            pct_hint=_fnum(row.get("f3")),
            change_hint=_fnum(row.get("f4")),
            volume=_fnum(row.get("f5")),
            amount=_fnum(row.get("f6")),
            volume_ratio=_fnum(row.get("f10")),
            high=_fnum(row.get("f15")),
            low=_fnum(row.get("f16")),
            open=_fnum(row.get("f17")),
            prev_close=_fnum(row.get("f18")),
            turnover=_fnum(row.get("f8")),
            pe=_fnum(row.get("f9")),
            cap=_fnum(row.get("f20")),
            main_net=_fnum(row.get("f62")),
            main_net_pct=_fnum(row.get("f184")),
            super_net=_fnum(row.get("f66")),
            super_net_pct=_fnum(row.get("f69")),
            big_net=_fnum(row.get("f72")),
            big_net_pct=_fnum(row.get("f75")),
            mid_net=_fnum(row.get("f78")),
            mid_net_pct=_fnum(row.get("f81")),
            small_net=_fnum(row.get("f84")),
            small_net_pct=_fnum(row.get("f87")),
        )
    return out


def fetch_quotes_sina(secids: List[str], timeout: float = 8.0) -> Dict[str, Quote]:
    """新浪兜底，仅支持沪深京。"""
    symbol_map: Dict[str, str] = {}
    for secid in secids:
        mk, _, code = secid.partition(".")
        if mk == "1":
            symbol_map[f"sh{code}"] = secid
        elif mk == "0":
            symbol_map[f"sz{code}"] = secid
    if not symbol_map:
        return {}
    url = SINA_URL + ",".join(symbol_map.keys())
    text = _http_get(url, referer="https://finance.sina.com.cn", timeout=timeout).decode("gb18030", "replace")
    out: Dict[str, Quote] = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        symbol, body = m.group(1), m.group(2)
        parts = body.split(",")
        if len(parts) < 32:
            continue
        secid = symbol_map.get(symbol)
        if not secid:
            continue
        out[secid] = Quote(
            secid=secid,
            code=secid.split(".")[1],
            name=parts[0],
            open=_fnum(parts[1]),
            prev_close=_fnum(parts[2]),
            price=_fnum(parts[3]),
            high=_fnum(parts[4]),
            low=_fnum(parts[5]),
            volume=_fnum(parts[8]) / 100.0,
            amount=_fnum(parts[9]),
            quote_time=parts[31] if len(parts) > 31 else "",
            date_hint=parts[30] if len(parts) > 30 else "",
        )
    return out


def fetch_commodities(items: Optional[List[Dict[str, str]]] = None,
                      timeout: float = 10.0) -> Dict[str, Commodity]:
    """贵金属 / 原油 / 外汇等通货行情。

    数据源（实测）：
      · 新浪 hq.sinajs.cn 一次请求可取全部 hf_（外盘）/ nf_（国内期货）/ fx_（外汇）
      · 东财 ulist：美元指数（100.UDI）等新浪没有的，用数字代码走兜底
    字段布局（实测确认）：
      hf_  15 或 14 段：0现价 4最高 5最低 6时间 7昨收 8开盘 12日期 13名称
      nf_  44 段：0名称 1时间HHMMSS 2开 3高 4低 8最新 10昨结算
      fx_  18 段：0时间 1买 2卖 3昨收 8最新 9名称 10涨跌% 11涨跌额 17日期
    """
    if items is None:
        items = DEFAULT_CONFIG["commodities"]
    items = [it for it in (items or []) if isinstance(it, dict) and it.get("code")]
    if not items:
        return {}

    cache_key = "commodity_" + "_".join(sorted(it["code"] for it in items))
    hit = CACHE.get(cache_key, ttl=5)
    # 只认非空缓存：空列表要落到下面走实时取数 + 快照兜底
    if hit and hit[0]:
        try:
            return {c["code"]: Commodity(**c) for c in hit[0]}
        except Exception:
            pass

    out: Dict[str, Commodity] = {}
    sina_codes = [it["code"] for it in items if not it["code"][:1].isdigit()]
    em_codes = [it["code"] for it in items if it["code"][:1].isdigit()]
    meta = {it["code"]: it for it in items}

    # ---- 新浪：一次请求 ---- 
    if sina_codes and LIMITER.allow("commodity:sina"):
        try:
            url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
            text = _http_get(url, referer="https://finance.sina.com.cn/",
                             timeout=timeout).decode("gb18030", "replace")
            LIMITER.success("commodity:sina")
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("var hq_str_"):
                    continue
                head, _, tail = line.partition("=")
                code = head[len("var hq_str_"):].strip()
                body = tail.strip().rstrip(";").strip('"')
                if not body or code not in meta:
                    continue
                parsed = _parse_commodity(code, body, meta[code])
                if parsed:
                    out[code] = parsed
        except Exception:  # noqa: BLE001
            LIMITER.failure("commodity:sina")

    # ---- 东财：新浪没有的（美元指数等），主站被限流时换备用域名 ----
    for code in em_codes:
        if code in out or not LIMITER.allow("commodity:em"):
            continue
        query = urllib.parse.urlencode({
            "secids": code, "fields": "f12,f13,f14,f2,f3,f4,f15,f16,f18,f17",
            "fltt": "2", "invt": "2", "np": "1", "ut": UT_TOKEN,
            "_": str(int(time.time() * 1000))})
        payload = None
        for base in (EM_QUOTE_URL, EM_QUOTE_URL.replace("push2.", "push2delay.")):
            if _host_blocked(base):
                continue
            try:
                payload = json.loads(_http_get(f"{base}?{query}", timeout=timeout)
                                     .decode("utf-8", "replace"))
                LIMITER.success("commodity:em")
                break
            except Exception:  # noqa: BLE001
                LIMITER.failure("commodity:em")
                payload = None
        if payload is None:
            continue
        try:
            diff = ((payload or {}).get("data") or {}).get("diff") or []
            if not diff:
                continue
            row = diff[0]
            price = _fnum(row.get("f2"))
            prev = _fnum(row.get("f18"))
            out[code] = Commodity(
                code=code,
                name=str(meta[code].get("name") or row.get("f14") or ""),
                price=price, prev_close=prev,
                open=_fnum(row.get("f17")), high=_fnum(row.get("f15")),
                low=_fnum(row.get("f16")),
                change=(price - prev) if (price and prev) else _fnum(row.get("f4")),
                pct=_fnum(row.get("f3")),
                unit=str(meta[code].get("unit") or ""),
                source="东财",
            )
        except Exception:  # noqa: BLE001
            pass

    if out:
        CACHE.put(cache_key, [c.__dict__ for c in out.values()])
        # 长期快照：某个源临时不可用时用来兜底展示（标 stale，不谎报实时）
        snap_key = "commodity_snapshot"
        snap = (CACHE.get_stale(snap_key) or (None, 0))[0] or {}
        try:
            for code, item in out.items():
                snap[code] = item.__dict__
            CACHE.put(snap_key, snap)
        except Exception:
            pass

    # 缺失项用快照补上（标 stale），避免固定几个位置长期显示 --
    missing = [it for it in items if it["code"] not in out]
    if missing:
        snap = (CACHE.get_stale("commodity_snapshot") or (None, 0))[0] or {}
        for it in missing:
            saved = snap.get(it["code"])
            if not saved:
                continue
            try:
                item = Commodity(**{k: v for k, v in saved.items()
                                    if k in Commodity.__dataclass_fields__})
                item.stale = True
                if it.get("name"):
                    item.name = str(it["name"])
                out[it["code"]] = item
            except Exception:
                continue
    return out


def _parse_commodity(code: str, body: str, meta: Dict[str, str]) -> Optional[Commodity]:
    """按 code 前缀解析新浪返回的三类格式。"""
    parts = body.split(",")
    name_hint = str(meta.get("name") or "")
    unit = str(meta.get("unit") or "")

    def f(idx: float = 0) -> float:
        i = int(idx)
        return _fnum(parts[i]) if 0 <= i < len(parts) else 0.0

    if code.startswith("hf_"):
        if len(parts) < 13:
            return None
        price, prev, open_, high, low = f(0), f(7), f(8), f(4), f(5)
        quote_time = parts[6] if len(parts) > 6 else ""
        real_name = parts[13].strip() if len(parts) > 13 else ""
    elif code.startswith("nf_"):
        if len(parts) < 18:
            return None
        price, prev, open_, high, low = f(8), f(10), f(2), f(3), f(4)
        raw_time = parts[1] if len(parts) > 1 else ""
        quote_time = (f"{raw_time[0:2]}:{raw_time[2:4]}:{raw_time[4:6]}"
                      if len(raw_time) >= 6 else raw_time)
        real_name = parts[0].strip()
    elif code.startswith("fx_"):
        if len(parts) < 12:
            return None
        price, prev, open_, high, low = f(8), f(3), f(5), f(6), f(7)
        quote_time = parts[0] if parts else ""
        real_name = parts[9].strip() if len(parts) > 9 else ""
    else:
        return None

    if not price:
        return None
    change = _fnum(parts[11]) if code.startswith("fx_") and len(parts) > 11 else 0.0
    if not change and prev:
        change = price - prev
    pct = _fnum(parts[10]) if code.startswith("fx_") and len(parts) > 10 else 0.0
    if not pct and prev:
        pct = (price - prev) / prev * 100.0
    return Commodity(
        code=code,
        # 优先用配置里的中文标签（如"沪金主力"），比接口返回的"黄金连续"更直观
        name=name_hint or real_name,
        price=price, prev_close=prev, open=open_, high=high, low=low,
        change=change, pct=pct, unit=unit, quote_time=quote_time, source="新浪",
    )


class QuoteClient:
    """统一行情入口，带来源降级 + 限速 + 磁盘快照。"""

    def __init__(self):
        self.last_source = ""
        self.last_error = ""
        self.last_from_cache = False

    def fetch(self, secids: List[str], timeout: float = 8.0) -> Dict[str, Quote]:
        secids = [s for s in dict.fromkeys(secids) if s]
        if not secids:
            return {}
        self.last_from_cache = False
        key = "quotes_" + "_".join(sorted(secids))[:120]
        if LIMITER.allow("quotes:em"):
            try:
                data = fetch_quotes_eastmoney(secids, timeout=timeout)
                if data:
                    LIMITER.success("quotes:em")
                    self.last_source = "东方财富"
                    self.last_error = ""
                    CACHE.put(key, [q.__dict__ for q in data.values()])
                    return data
            except Exception as exc:  # noqa: BLE001
                LIMITER.failure("quotes:em")
                self.last_error = f"东财: {type(exc).__name__}"
        else:
            self.last_error = "东财: 限速中"
        if LIMITER.allow("quotes:sina"):
            try:
                data = fetch_quotes_sina(secids, timeout=timeout)
                if data:
                    LIMITER.success("quotes:sina")
                    self.last_source = "新浪"
                    self.last_error = ""
                    CACHE.put(key, [q.__dict__ for q in data.values()])
                    return data
            except Exception as exc:  # noqa: BLE001
                LIMITER.failure("quotes:sina")
                self.last_error = (self.last_error + f" / 新浪: {type(exc).__name__}").strip(" /")
        else:
            self.last_error = (self.last_error + " / 新浪: 限速中").strip(" /")
        # 两个源都不可用：退到磁盘快照（界面显示"最后已知价格"而不是一片 --）
        stale = CACHE.get_stale(key)
        if stale:
            try:
                quotes = {q["secid"]: Quote(**q) for q in stale[0]}
                self.last_source = f"缓存({int(stale[1] / 60)}分钟前)"
                self.last_from_cache = True
                return quotes
            except Exception:
                pass
        return {}

    def search(self, keyword: str, timeout: float = 6.0) -> List[Dict[str, str]]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        params = {
            "input": kw,
            "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": "12",
            "_": str(int(time.time() * 1000)),
        }
        url = EM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
        try:
            raw = _http_get(url, timeout=timeout).decode("utf-8", "replace")
        except Exception:
            return []
        items = _parse_search_payload(raw)
        out: List[Dict[str, str]] = []
        for row in items:
            market = str(row.get("MktNum") or row.get("mktnum") or "")
            code = str(row.get("Code") or row.get("code") or "")
            name = str(row.get("Name") or row.get("name") or "")
            sec_type = str(row.get("SecurityType") or row.get("securitytype") or "")
            if not code:
                continue
            if sec_type and not sec_type.startswith(("1", "2", "3", "7", "8")):
                continue
            if market in ("0", "1") and not re.match(r"^\d{6}$", code):
                continue
            if market in ("105", "106", "107") and not re.match(r"^[A-Za-z.\-]{1,10}$", code):
                continue
            if not market:
                market = str(market_of(code)) if re.match(r"^\d{6}$", code) else ""
            if not market:
                continue
            out.append({"secid": f"{market}.{code}", "code": code, "name": name})
        return out[:12]


def _parse_search_payload(raw: str) -> List[Dict[str, Any]]:
    """东财搜索接口有时返回 QuotationCodeTable.Data，有时直接返回 Data。"""
    text = raw.strip()
    m = re.match(r"^[\w$]+\((.*)\);?$", text, re.S)  # jsonp 包装
    if m:
        text = m.group(1)
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    table = payload.get("QuotationCodeTable")
    if isinstance(table, dict) and isinstance(table.get("Data"), list):
        return table["Data"]
    if isinstance(payload.get("Data"), list):
        return payload["Data"]
    return []


# --------------------------------------------------------------------------
# K 线 / 资金流历史
# --------------------------------------------------------------------------
@dataclass
class Bar:
    """一根 K 线。"""
    date: str = ""
    open: float = 0.0
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0       # 手
    amount: float = 0.0       # 元
    amplitude: float = 0.0    # 振幅 %
    pct: float = 0.0          # 涨跌幅 %
    change: float = 0.0       # 涨跌额
    turnover: float = 0.0     # 换手率 %
    # ---- 资金流历史专用（元）----
    main_net: float = 0.0     # 主力净流入
    super_net: float = 0.0    # 超大单净额
    big_net: float = 0.0      # 大单净额
    mid_net: float = 0.0      # 中单净额
    small_net: float = 0.0    # 小单净额

    @property
    def is_up(self) -> bool:
        return self.close >= self.open

    @property
    def label(self) -> str:
        """横轴/提示用的短标签。"""
        text = self.date
        if " " in text:
            return text.split(" ")[1][:5]
        return text[5:] if len(text) >= 10 else text


@dataclass
class KlineChart:
    name: str = ""
    code: str = ""
    bars: List[Bar] = field(default_factory=list)
    period: str = "daily"
    adjust: int = 1
    secid: str = ""
    source: str = ""

    def ma(self, n: int) -> List[Optional[float]]:
        closes = [b.close for b in self.bars]
        out: List[Optional[float]] = []
        total = 0.0
        for idx, close in enumerate(closes):
            total += close
            if idx >= n:
                total -= closes[idx - n]
            out.append(total / n if idx >= n - 1 else None)
        return out


KLT_CHOICES: Dict[str, int] = {
    "1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60,
    "daily": 101, "weekly": 102, "monthly": 103,
}
KLT_LABEL = {
    "tick": "分时",
    "1min": "分时", "5min": "5分", "15min": "15分", "30min": "30分", "60min": "60分",
    "daily": "日K", "weekly": "周K", "monthly": "月K",
}
# 分时（当日走势）用的周期键：与 1min 区分开，画折线而不是蜡烛
PERIOD_TICK = "tick"
PERIOD_TICK_LABEL = "分时"
FQT_LABEL = {0: "不复权", 1: "前复权", 2: "后复权"}


# --------------------------------------------------------------------------
# Tushare Pro 数据源（可选；装了 tushare 包并配置 token 才启用）
#
# 分工说明：Tushare **没有实时逐笔行情**（daily 是日线级），所以
#   · 实时现价/涨跌  → 仍走新浪/东财
#   · K 线、资金流   → Tushare 优先，公开接口兜底
# 设计要点：懒加载（不装包也能跑）、失败就静默降级、按积分权限探测可用接口。
# --------------------------------------------------------------------------
_TUSHARE_STATE: Dict[str, Any] = {"api": None, "tried": False, "token": "",
                                 "reason": "", "mode": ""}
TUSHARE_API_URL = "http://api.tushare.pro"


def tushare_status() -> Dict[str, Any]:
    """返回 Tushare 的启用状态（供界面/自检显示）。"""
    return {
        "installed": _tushare_installed(),
        "token_configured": bool(_TUSHARE_STATE.get("token")),
        "api_ready": _TUSHARE_STATE.get("api") is not None,
        "mode": _TUSHARE_STATE.get("mode", ""),      # sdk | http
        "reason": _TUSHARE_STATE.get("reason", ""),
    }


def _tushare_installed() -> bool:
    try:
        import tushare  # noqa: F401
        return True
    except Exception:
        return False


class TushareHttp:
    """Tushare Pro 的 HTTP 直连实现（等价于官方 SDK 的一层 POST 封装）。

    官方 SDK 本质就是对 http://api.tushare.pro 发 POST；这里直接用已装好的
    requests 调，好处是不必再 pip 装 tushare 包。装了包时优先用包。
    """

    def __init__(self, token: str, timeout: float = 15.0) -> None:
        self.token = token
        self.timeout = timeout

    def query(self, api_name: str, fields: str = "", **params: Any):
        import requests
        payload = {"api_name": api_name, "token": self.token,
                   "params": params, "fields": fields}
        resp = requests.post(TUSHARE_API_URL, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"tushare {api_name}: {body.get('msg')}")
        data = body.get("data") or {}
        fields_out = data.get("fields") or []
        items = data.get("items") or []
        # 返回与 pandas 类似的结构（dict 列表），避免上层依赖 pandas
        return [dict(zip(fields_out, row)) for row in items]


def init_tushare(token: str, enabled: bool = True) -> bool:
    """初始化 Tushare。返回是否可用；任何异常都不抛出（不影响其它数据源）。"""
    _TUSHARE_STATE.update({"api": None, "tried": True, "token": token or "",
                           "reason": "", "mode": ""})
    if not enabled:
        _TUSHARE_STATE["reason"] = "已关闭"
        return False
    if not token:
        _TUSHARE_STATE["reason"] = "未配置 token"
        return False
    # 1) 官方 SDK（装了就用）
    if _tushare_installed():
        try:
            import tushare as ts
            ts.set_token(token)
            _TUSHARE_STATE["api"] = ts.pro_api(token)
            _TUSHARE_STATE["mode"] = "sdk"
            return True
        except Exception as exc:  # noqa: BLE001
            _TUSHARE_STATE["reason"] = f"SDK 初始化失败 {type(exc).__name__}: {exc}"
    # 2) HTTP 直连（无需安装 tushare 包）
    try:
        _TUSHARE_STATE["api"] = TushareHttp(token)
        _TUSHARE_STATE["mode"] = "http"
        _TUSHARE_STATE["reason"] = "" if _tushare_installed() else "（使用 HTTP 直连，未安装 tushare 包）"
        return True
    except Exception as exc:  # noqa: BLE001
        _TUSHARE_STATE["reason"] = f"初始化失败 {type(exc).__name__}: {exc}"
        return False


def _ts_api():
    return _TUSHARE_STATE.get("api")


def _ts_records(result: Any) -> List[Dict[str, Any]]:
    """把 SDK 的 DataFrame 或 HTTP 的 dict 列表统一成 dict 列表。"""
    if result is None:
        return []
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    try:
        if hasattr(result, "to_dict"):
            return list(result.to_dict("records"))
    except Exception:
        pass
    return []


def ts_code_of(secid: str) -> str:
    """secid(1.600519) -> Tushare 代码(600519.SH)。港股/美股 Tushare 覆盖不同，返回空串。"""
    mk, _, code = secid.partition(".")
    if mk == "1":
        return f"{code}.SH"
    if mk == "0":
        # 北交所 4/8 开头是 .BJ
        return f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ"
    return ""


def _ts_date(text: Any) -> str:
    """20261014 -> 2026-10-14"""
    raw = str(text or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


_KLT_TO_TS = {"daily": "D", "weekly": "W", "monthly": "M"}


def _fetch_kline_tushare(secid: str, period: str, adjust: int, limit: int) -> List[Bar]:
    """Tushare 日/周/月线。分钟线需要高积分接口，这里不做（交给公开源）。"""
    api = _ts_api()
    ts_code = ts_code_of(secid)
    if api is None or not ts_code or period not in _KLT_TO_TS:
        return []
    freq = _KLT_TO_TS[period]
    api_name = {"D": "daily", "W": "weekly", "M": "monthly"}[freq]
    adj = {0: None, 1: "qfq", 2: "hfq"}.get(adjust)
    need = max(20, limit)
    end = datetime.now().strftime("%Y%m%d")
    # 取足够长区间（考虑停牌与复权需要前置数据）
    span = {"D": 2.0, "W": 10.0, "M": 40.0}[freq]
    start = (datetime.now() - timedelta(days=int(need * span) + 40)).strftime("%Y%m%d")
    fields = "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,change"
    rows: List[Dict[str, Any]] = []
    if isinstance(api, TushareHttp):
        rows = _ts_records(api.query(api_name, fields=fields, ts_code=ts_code,
                                     start_date=start, end_date=end))
    else:
        try:
            frame = api.query(api_name, ts_code=ts_code, start_date=start, end_date=end)
            rows = _ts_records(frame)
        except Exception:
            rows = []
        # 复权：SDK 有 pro_bar 更省事
        if adj and not rows:
            try:
                import tushare as ts
                rows = _ts_records(ts.pro_bar(ts_code=ts_code, freq=freq, adj=adj, limit=need))
            except Exception:
                rows = []
    if not rows and not isinstance(api, TushareHttp):
        try:
            rows = _ts_records(api.query(api_name, ts_code=ts_code, limit=need))
        except Exception:
            rows = []
    bars: List[Bar] = []
    for row in rows:
        close = _fnum(row.get("close"))
        if not close:
            continue
        bars.append(Bar(
            date=_ts_date(row.get("trade_date")),
            open=_fnum(row.get("open")),
            high=_fnum(row.get("high")),
            low=_fnum(row.get("low")),
            close=close,
            volume=_fnum(row.get("vol")),                 # 单位：手
            amount=_fnum(row.get("amount")) * 1000.0,     # Tushare 单位千元 -> 元
            pct=_fnum(row.get("pct_chg")),
            change=_fnum(row.get("change")),
        ))
    if bars:
        bars.sort(key=lambda b: b.date)            # Tushare 返回是倒序
    return bars[-need:] if need else bars


def _fetch_fundflow_tushare(secid: str, limit: int) -> List[Bar]:
    """Tushare 个股资金流（moneyflow）。

    字段与单位（实测）：
      · 金额字段是**万元**（net_mf_amount=62240.29 即 6.22 亿），需 ×1e4
      · 超大单 = elg、大单 = lg、中单 = md、小单 = sm
      · 没有 close 字段，收盘价由日线补齐（拿不到就不填）
    """
    api = _ts_api()
    ts_code = ts_code_of(secid)
    if api is None or not ts_code:
        return []
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(45, limit * 5))).strftime("%Y%m%d")
    fields = ("ts_code,trade_date,net_mf_amount,"
              "buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
              "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount")
    try:
        if isinstance(api, TushareHttp):
            rows = _ts_records(api.query("moneyflow", fields=fields, ts_code=ts_code,
                                         start_date=start, end_date=end))
        else:
            rows = _ts_records(api.query("moneyflow", ts_code=ts_code,
                                         start_date=start, end_date=end))
    except Exception:
        return []
    if not rows:
        return []
    # 收盘价：用日线补齐，方便表格显示
    closes: Dict[str, float] = {}
    try:
        for k in _fetch_kline_tushare(secid, "daily", 1, max(30, limit)):
            closes[k.date] = k.close
    except Exception:
        pass

    W = 1e4        # 万元 -> 元
    out: List[Bar] = []
    for row in rows:
        date = _ts_date(row.get("trade_date"))
        net_xl = (_fnum(row.get("buy_elg_amount")) - _fnum(row.get("sell_elg_amount"))) * W
        net_l = (_fnum(row.get("buy_lg_amount")) - _fnum(row.get("sell_lg_amount"))) * W
        net_md = (_fnum(row.get("buy_md_amount")) - _fnum(row.get("sell_md_amount"))) * W
        net_sm = (_fnum(row.get("buy_sm_amount")) - _fnum(row.get("sell_sm_amount"))) * W
        net_main = _fnum(row.get("net_mf_amount")) * W
        if not net_main:
            net_main = net_xl + net_l
        out.append(Bar(
            date=date,
            main_net=net_main,
            super_net=net_xl,
            big_net=net_l,
            mid_net=net_md,
            small_net=net_sm,
            close=closes.get(date, 0.0),
        ))
    out.sort(key=lambda b: b.date)
    return out[-limit:] if limit else out


def _parse_bar(line: str) -> Optional[Bar]:
    parts = line.split(",")
    if len(parts) < 6:
        return None
    bar = Bar(
        date=parts[0],
        open=_fnum(parts[1]),
        close=_fnum(parts[2]),
        high=_fnum(parts[3]),
        low=_fnum(parts[4]),
        volume=_fnum(parts[5]),
    )
    if len(parts) > 6:
        bar.amount = _fnum(parts[6])
    if len(parts) > 7:
        bar.amplitude = _fnum(parts[7])
    if len(parts) > 8:
        bar.pct = _fnum(parts[8])
    if len(parts) > 9:
        bar.change = _fnum(parts[9])
    if len(parts) > 10:
        bar.turnover = _fnum(parts[10])
    return bar


def _tx_symbol(secid: str) -> str:
    mk, _, code = secid.partition(".")
    if mk == "1":
        return "sh" + code
    if mk == "0":
        return "sz" + code
    if mk == "116":
        return "hk" + code
    if mk in ("105", "106", "107"):
        return "us" + code
    return ""


_SINA_SCALE = {"1min": 5, "5min": 5, "15min": 15, "30min": 30, "60min": 60,
               "daily": 240, "weekly": 1200, "monthly": 7200}


def _parse_tx_kline(raw: bytes, chart: KlineChart, period: str) -> List[Bar]:
    payload = json.loads(raw.decode("utf-8", "replace"))
    node = (payload.get("data") or {})
    first = next(iter(node.values()), None) if isinstance(node, dict) else None
    if not isinstance(first, dict):
        return []
    keys = ["day"] if period == "daily" else (
        ["week"] if period == "weekly" else (["month"] if period == "monthly" else ["min"]))
    rows = None
    for prefix in ("qfq", "hfq", ""):
        for key in keys:
            candidate = first.get(prefix + key)
            if candidate:
                rows = candidate
                break
        if rows:
            break
    if not rows:
        for key, value in first.items():
            if isinstance(value, list) and value and isinstance(value[0], list) and len(value[0]) >= 6:
                rows = value
                break
    bars: List[Bar] = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        bar = Bar(date=str(row[0]), open=_fnum(row[1]), close=_fnum(row[2]),
                  high=_fnum(row[3]), low=_fnum(row[4]), volume=_fnum(row[5]))
        if len(row) > 6 and not isinstance(row[6], (list, dict)):
            bar.amount = _fnum(row[6])
        bars.append(bar)
    # 计算涨跌幅（腾讯不直接给）
    prev: Optional[float] = None
    for bar in bars:
        if prev:
            bar.change = bar.close - prev
            bar.pct = (bar.close - prev) / prev * 100.0
        prev = bar.close
    return bars


def _parse_sina_kline(raw: bytes, period: str) -> List[Bar]:
    text = raw.decode("utf-8", "replace").strip()
    if not text or text in ("null", "[]"):
        return []
    try:
        rows = json.loads(text)
    except Exception:
        return []
    bars: List[Bar] = []
    for row in rows if isinstance(rows, list) else []:
        bar = Bar(date=str(row.get("day", "")), open=_fnum(row.get("open")),
                  close=_fnum(row.get("close")), high=_fnum(row.get("high")),
                  low=_fnum(row.get("low")), volume=_fnum(row.get("volume")) / 100.0)
        bars.append(bar)
    prev: Optional[float] = None
    for bar in bars:
        if prev:
            bar.change = bar.close - prev
            bar.pct = (bar.close - prev) / prev * 100.0
        prev = bar.close
    return bars


def _sina_symbol(secid: str) -> str:
    """新浪分时/K线用的 symbol（sh600519 / sz300750 / hk00700 / gb_aapl）。"""
    return _sina_kline_symbol(secid)[0]


def fetch_tick_day(secid: str, timeout: float = 12.0) -> KlineChart:
    """当日分时走势（1 分钟，只保留今天）。

    踩坑记录：新浪 minute 接口**一个交易日只有约 240 个点**，
    如果直接按 datalen=320 请求，会把昨天下午的数据也带进来，
    图上就变成"昨天+今天"两段 —— 看着就是错的。所以要多取一些再按日期过滤。
    """
    symbol = _sina_symbol(secid)
    chart = KlineChart(secid=secid, period=PERIOD_TICK, source="新浪")
    if not symbol or not symbol[:2] in ("sh", "sz"):
        return chart
    cache_key = f"tick_{secid}"
    hit = CACHE.get(cache_key, ttl=CACHE_TTL["tick"])
    if hit:
        data, age = hit
        chart.bars = [Bar(**b) for b in data["bars"]]
        chart.name = data.get("name", "")
        chart.source = f"新浪(缓存{int(age)}s)"
        return chart
    if not LIMITER.allow("tick:sina"):
        stale = CACHE.get_stale(cache_key)
        if stale:
            chart.bars = [Bar(**b) for b in stale[0]["bars"]]
            chart.name = stale[0].get("name", "")
            chart.source = f"新浪(限速,{int(stale[1])}s前)"
        return chart
    url = SINA_MIN1_URL + "?" + urllib.parse.urlencode(
        {"symbol": symbol, "scale": "1", "ma": "no", "datalen": "500"})
    try:
        bars = _parse_sina_kline(_http_get(url, referer="https://finance.sina.com.cn",
                                           timeout=timeout), "1min")
    except Exception:  # noqa: BLE001
        LIMITER.failure("tick:sina")
        stale = CACHE.get_stale(cache_key)
        if stale:
            chart.bars = [Bar(**b) for b in stale[0]["bars"]]
            chart.source = f"新浪(失败,{int(stale[1])}s前)"
        return chart
    LIMITER.success("tick:sina")
    if not bars:
        return chart
    today = datetime.now().strftime("%Y-%m-%d")
    todays = [b for b in bars if str(b.date).startswith(today)]
    stale_day = False
    if not todays:
        # 今天完全没有数据：可能是停牌，也可能是早盘前。
        # 展示最近一个有数据的交易日，并打上标记（不要留一片空白）。
        last_day = str(bars[-1].date)[:10]
        todays = [b for b in bars if str(b.date).startswith(last_day)]
        stale_day = bool(todays)
    chart.bars = todays
    chart.name = _name_of_secid(secid)
    chart.source = "新浪"
    if stale_day:
        chart.source = f"新浪({todays[0].date if todays else ''} 非当日)"
    CACHE.put(cache_key, {
        "bars": [{"date": b.date, "open": b.open, "close": b.close, "high": b.high,
                  "low": b.low, "volume": b.volume, "amount": b.amount, "pct": b.pct}
                 for b in todays],
        "name": chart.name, "source": chart.source,
    })
    return chart


def fetch_fundflow_intraday(secid: str, timeout: float = 10.0) -> List[Bar]:
    """当日资金流分时（东财 fflow/kline klt=1）：每分钟的累计净流入。

    用于详情页"当日主力"的走势，其它源（新浪 MoneyFlow 只有日级）替代不了。
    """
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": "1", "lmt": "0", "ut": UT_TOKEN,
        "_": str(int(time.time() * 1000)),
    }
    url = EM_FFLOW_KLINE_URL + "?" + urllib.parse.urlencode(params)
    cache_key = f"flowintraday_{secid}"
    hit = CACHE.get(cache_key, ttl=CACHE_TTL["flow_intraday"])
    # 只认非空缓存：空列表要落到下面走实时取数（与 fetch_commodities 同一处理）
    if hit and hit[0]:
        return [Bar(**b) for b in hit[0]]
    payload = None
    if not _host_blocked(url) and LIMITER.allow("flowintraday:em"):
        try:
            payload = json.loads(_http_get(url, timeout=timeout).decode("utf-8", "replace"))
            LIMITER.success("flowintraday:em")
        except Exception:  # noqa: BLE001
            LIMITER.failure("flowintraday:em")
            payload = None
    if payload is None:
        stale = CACHE.get_stale(cache_key)
        if stale:
            return [Bar(**b) for b in stale[0]]
        # 东财不可用时返回空，由上层用日级资金流展示
        return []
    data = (payload or {}).get("data") or {}
    out: List[Bar] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        # 字段顺序：时间, 主力净流入, 小单, 中单, 大单, 超大单
        out.append(Bar(date=parts[0], main_net=_fnum(parts[1]), small_net=_fnum(parts[2]),
                       mid_net=_fnum(parts[3]), big_net=_fnum(parts[4]),
                       super_net=_fnum(parts[5])))
    if out:
        CACHE.put(cache_key, [b.__dict__ for b in out])
    return out


def fetch_fundflow_today_sina(secid: str, timeout: float = 10.0) -> Optional[Bar]:
    """新浪当日资金流（备用源）。返回最近一个交易日的净额。"""
    symbol = _sina_symbol(secid)
    if not symbol:
        return None
    code = symbol if symbol[:2] in ("sh", "sz") else symbol
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_qsfx_zjlrqs?" + urllib.parse.urlencode(
               {"page": "1", "num": "1", "sort": "opendate", "asc": "0", "daima": code}))
    try:
        rows = json.loads(_http_get(url, referer="https://finance.sina.com.cn",
                                    timeout=timeout).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    # netamount 是全部净额、r0_net 是超大单净额（新浪口径）
    return Bar(
        date=str(row.get("opendate") or ""),
        main_net=_fnum(row.get("netamount")),
        super_net=_fnum(row.get("r0_net")),
        close=_fnum(row.get("trade")),
        pct=_fnum(row.get("changeratio")) * 100.0,
        turnover=_fnum(row.get("turnover")),
    )


def _fetch_kline_em(secid: str, period: str, adjust: int, limit: int,
                    timeout: float, base: str) -> List[Bar]:
    klt = KLT_CHOICES.get(period, 101)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": str(klt), "fqt": str(adjust), "beg": "0", "end": "20500101",
        "lmt": str(max(10, min(limit, 1000))), "ut": UT_TOKEN,
        "_": str(int(time.time() * 1000)),
    }
    url = base + "?" + urllib.parse.urlencode(params)
    payload = json.loads(_http_get(url, timeout=timeout).decode("utf-8", "replace"))
    data = (payload or {}).get("data") or {}
    out: List[Bar] = []
    for line in data.get("klines") or []:
        bar = _parse_bar(str(line))
        if bar:
            out.append(bar)
    return out


def _fetch_kline_tx(secid: str, period: str, adjust: int, limit: int,
                    timeout: float) -> List[Bar]:
    tx_symbol = _tx_symbol(secid)
    if not tx_symbol:
        return []
    tx_period = {"1min": "m1", "5min": "m5", "15min": "m15", "30min": "m30",
                 "60min": "m60", "daily": "day", "weekly": "week",
                 "monthly": "month"}.get(period, "day")
    # 腾讯的「时间区间留空 + 条数」在港美股上只会回 1~2 条，条数按倍率放宽更稳
    want = max(60, min(limit * 3, 1200))
    fq = {0: "bfq", 1: "qfq", 2: "hfq"}.get(adjust, "qfq")
    param = f"{tx_symbol},{tx_period},,,{want},{fq}"
    url = TX_KLINE_URL + "?" + urllib.parse.urlencode({"param": param})
    bars = _parse_tx_kline(_http_get(url, timeout=timeout), KlineChart(), tx_period)
    return bars


def _sina_kline_symbol(secid: str) -> Tuple[str, str]:
    """返回 (新浪 K 线 symbol, 新浪五档 symbol)。港股/美股新浪用 hk / gb_ 前缀。"""
    mk, _, code = secid.partition(".")
    if mk == "1":
        return f"sh{code}", f"sh{code}"
    if mk == "0":
        return f"sz{code}", f"sz{code}"
    if mk == "116":
        return f"hk{code}", f"hk{code}"
    if mk in ("105", "106", "107"):
        return f"gb_{code.lower()}", f"gb_{code.lower()}"
    return "", ""


def _fetch_kline_sina(secid: str, period: str, limit: int, timeout: float) -> List[Bar]:
    if period not in _SINA_SCALE:
        return []
    symbol, _ = _sina_kline_symbol(secid)
    if not symbol:
        return []
    # 1 分钟走专用端点（CN_MarketDataService）并显式用 scale=1；
    # 其它周期走 CN_MarketData + 对应 scale。注意不能沿用 _SINA_SCALE['1min']（那是 5）。
    scale = 1 if period == "1min" else _SINA_SCALE[period]
    base = SINA_MIN1_URL if period == "1min" else SINA_KLINE_URL
    params = {"symbol": symbol, "scale": str(scale), "ma": "no",
              "datalen": str(max(30, min(limit, 1000)))}
    url = base + "?" + urllib.parse.urlencode(params)
    return _parse_sina_kline(_http_get(url, referer="https://finance.sina.com.cn",
                                       timeout=timeout), period)


def _kline_providers(secid: str, period: str, adjust: int, limit: int, timeout: float,
                     include_em: bool = True) -> List[Tuple[str, Any]]:
    """数据源顺序：Tushare（若已配置）→ 东财 →（分钟线优先新浪；日/周/月优先腾讯）。

    Tushare 的分钟线需要高积分接口，这里只用它的日/周/月线。
    """
    providers: List[Tuple[str, Any]] = []
    if _ts_api() is not None and period in _KLT_TO_TS:
        providers.append(("Tushare", lambda: _fetch_kline_tushare(secid, period, adjust, limit)))
    if include_em:
        providers += [
            ("东财", lambda: _fetch_kline_em(secid, period, adjust, limit, timeout, EM_KLINE_URL)),
            ("东财备用", lambda: _fetch_kline_em(secid, period, adjust, limit, timeout, EM_KLINE_MIRROR)),
        ]
    sina = ("新浪", lambda: _fetch_kline_sina(secid, period, limit, timeout))
    tx = ("腾讯", lambda: _fetch_kline_tx(secid, period, adjust, limit, timeout))
    if period in ("1min", "5min", "15min", "30min", "60min"):
        providers += [sina, tx]
    else:
        providers += [tx, sina]
    return providers


def fetch_kline(secid: str, period: str = "daily", adjust: int = 1,
                limit: int = 180, timeout: float = 10.0) -> KlineChart:
    """拉取 K 线（多源降级 + 磁盘缓存）。

    顺序：Tushare（若配置）→ 东财 push2his → 东财 push2delay →（分钟线优先新浪 / 日周月优先腾讯）
    缓存：日/周/月线 15 分钟内不再请求；分钟线 45 秒。
    """
    chart = KlineChart(secid=secid, period=period, adjust=adjust)
    cache_key = f"kline_{secid}_{period}_{adjust}_{limit}"
    ttl = CACHE_TTL["kline_daily"] if period in ("daily", "weekly", "monthly") \
        else CACHE_TTL["kline_minute"]
    hit = CACHE.get(cache_key, ttl=ttl)
    if hit:
        data, age = hit
        try:
            chart.bars = [Bar(**b) for b in data["bars"]]
            chart.name = data.get("name", "")
            chart.source = f"{data.get('source', '')}(缓存{int(age)}s)"
            return chart
        except Exception:
            pass

    errors: List[str] = []
    providers = _kline_providers(secid, period, adjust, limit, timeout)
    allowed = [(n, f) for n, f in providers if LIMITER.allow(f"kline:{n}")]
    if not allowed:
        # 所有源都在限速/冷却窗口内：直接用缓存，别报错也别硬敲
        stale = CACHE.get_stale(cache_key)
        if stale:
            data, age = stale
            chart.bars = [Bar(**b) for b in data["bars"]]
            chart.name = data.get("name", "")
            chart.source = f"{data.get('source', '')}(限速中,{int(age)}s前)"
            return chart
        errors.append("全部数据源限速中")

    for name, fetcher in allowed:
        try:
            bars = fetcher()
        except Exception as exc:  # noqa: BLE001
            LIMITER.failure(f"kline:{name}")
            errors.append(f"{name}:{type(exc).__name__}")
            continue
        if bars and len(bars) >= min(5, limit):
            LIMITER.success(f"kline:{name}")
            chart.bars = bars[-limit:] if limit else bars
            chart.name = _name_of_secid(secid)
            chart.source = name
            CACHE.put(cache_key, {
                "bars": [{"date": b.date, "open": b.open, "close": b.close, "high": b.high,
                          "low": b.low, "volume": b.volume, "amount": b.amount,
                          "amplitude": b.amplitude, "pct": b.pct, "change": b.change,
                          "turnover": b.turnover} for b in chart.bars],
                "name": chart.name, "source": name,
            })
            return chart
        if bars:
            errors.append(f"{name}:数据过少({len(bars)})")
    # 全部失败：退到过期缓存（总比空图好）
    stale = CACHE.get_stale(cache_key)
    if stale:
        data, age = stale
        try:
            chart.bars = [Bar(**b) for b in data["bars"]]
            chart.name = data.get("name", "")
            chart.source = f"{data.get('source', '')}(过期缓存{int(age / 60)}分钟)"
            log(f"K线全部数据源不可用，使用过期缓存 {secid} {period} ({int(age)}s 前)")
            return chart
        except Exception:
            pass
    if errors:
        raise RuntimeError("K线数据源均不可用(" + "/".join(dict.fromkeys(errors)) + ")")
    return chart


def fetch_kline_sources(secid: str, period: str = "daily", adjust: int = 1,
                        limit: int = 180, timeout: float = 10.0,
                        include_em: bool = True) -> KlineChart:
    """指定数据源集合拉 K 线（用于测试降级链，include_em=False 可验证备用源）。"""
    chart = KlineChart(secid=secid, period=period, adjust=adjust)
    errors: List[str] = []
    for name, fetcher in _kline_providers(secid, period, adjust, limit, timeout, include_em):
        try:
            bars = fetcher()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}:{type(exc).__name__}")
            continue
        if bars and len(bars) >= min(5, limit):
            chart.bars = bars[-limit:] if limit else bars
            chart.name = _name_of_secid(secid)
            chart.source = name
            return chart
        if bars:
            errors.append(f"{name}:数据过少({len(bars)})")
    if errors:
        raise RuntimeError("K线数据源均不可用(" + "/".join(dict.fromkeys(errors)) + ")")
    return chart


_NAME_CACHE: Dict[str, str] = {}
_NAME_TRIED: set = set()


def _name_of_secid(secid: str) -> str:
    """取股票名（内存缓存；失败过就不再反复请求，避免浪费额度）。"""
    if secid in _NAME_CACHE:
        return _NAME_CACHE[secid]
    if secid in _NAME_TRIED:
        return ""                      # 之前没取到，本次不再重试
    name = ""
    if CACHE:
        hit = CACHE.get(f"name_{secid}", ttl=86400 * 7)
        if hit:
            _NAME_CACHE[secid] = hit[0]
            return hit[0]
    try:
        quotes = fetch_quotes_eastmoney([secid], timeout=6.0)
        for quote in quotes.values():
            if quote.name:
                name = quote.name
                break
    except Exception:
        name = ""
    _NAME_TRIED.add(secid)
    if name:
        _NAME_CACHE[secid] = name
        CACHE.put(f"name_{secid}", name)
    return name


def fetch_fundflow_history(secid: str, limit: int = 20, timeout: float = 10.0) -> List[Bar]:
    """历史每日资金流（主力/超大单/大单净额）。

    优先 Tushare（moneyflow，需要积分）；不可用时回退东财，主站被限流再试备用域名。
    """
    if _ts_api() is not None:
        try:
            bars = _fetch_fundflow_tushare(secid, limit)
            if bars:
                return bars
            log(f"Tushare 资金流无数据，回退东财 {secid}")
        except Exception as exc:  # noqa: BLE001
            log(f"Tushare 资金流失败({type(exc).__name__})，回退东财 {secid}")
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": str(max(5, min(limit, 120))),
        "klt": "101",
        "ut": UT_TOKEN,
        "_": str(int(time.time() * 1000)),
    }
    query = urllib.parse.urlencode(params)
    last_exc: Optional[Exception] = None
    payload: Any = None
    for base in (EM_FFLOW_URL, EM_FFLOW_URL.replace("push2his.", "push2delay.")):
        if _host_blocked(base):
            continue          # 已被冷却就不要再去敲门，避免延长封禁
        try:
            payload = json.loads(_http_get(f"{base}?{query}", timeout=timeout)
                                 .decode("utf-8", "replace"))
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            payload = None
    if payload is None:
        if last_exc:
            raise last_exc
        raise RuntimeError("资金流数据源冷却中")
    data = (payload or {}).get("data") or {}
    out: List[Bar] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 13:
            continue
        out.append(Bar(
            date=parts[0],
            main_net=_fnum(parts[1]),
            small_net=_fnum(parts[2]),
            mid_net=_fnum(parts[3]),
            big_net=_fnum(parts[4]),
            super_net=_fnum(parts[5]),
            close=_fnum(parts[11]),
            pct=_fnum(parts[12]),
        ))
    return out


def fetch_fundflow_detail(secid: str, timeout: float = 8.0) -> Quote:
    """单只股票的实时资金流明细。

    实测踩坑：`/api/qt/stock/get` 这个端点对资金流字段（f62/f66/f72…）大多返回 0，
    真正可靠的是批量端点 `/api/qt/ulist.np/get`（和主面板用的是同一个），所以改走它。
    任何异常都吞掉并返回空 Quote，避免调用方（自测/界面）因为限流而崩。
    """
    params = {
        "secids": secid,
        "fields": "f12,f13,f14,f43,f57,f58,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87",
        "fltt": "2", "invt": "2", "np": "1", "ut": UT_TOKEN,
        "_": str(int(time.time() * 1000)),
    }
    url = EM_QUOTE_URL + "?" + urllib.parse.urlencode(params)
    try:
        payload = json.loads(_http_get(url, timeout=timeout).decode("utf-8", "replace"))
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        row = diff[0] if diff else {}
    except Exception:  # noqa: BLE001  限流/冷却时返回空值而不是抛错
        row = {}
    return Quote(
        secid=secid,
        code=str(row.get("f57") or ""),
        name=str(row.get("f58") or ""),
        price=_fnum(row.get("f43")),
        main_net=_fnum(row.get("f62")),
        main_net_pct=_fnum(row.get("f184")),
        super_net=_fnum(row.get("f66")),
        super_net_pct=_fnum(row.get("f69")),
        big_net=_fnum(row.get("f72")),
        big_net_pct=_fnum(row.get("f75")),
        mid_net=_fnum(row.get("f78")),
        mid_net_pct=_fnum(row.get("f81")),
        small_net=_fnum(row.get("f84")),
        small_net_pct=_fnum(row.get("f87")),
    )


# --------------------------------------------------------------------------
# 交易时段
# --------------------------------------------------------------------------
def now_hhmm() -> dtime:
    return datetime.now().time()


def is_trading_time(ts: Optional[datetime] = None) -> bool:
    """A 股交易时段（含集合竞价，剔除午间休市）。"""
    dt = ts or datetime.now()
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return (dtime(9, 15) <= t <= dtime(11, 30)) or (dtime(12, 55) <= t <= dtime(15, 0))


def market_phase(ts: Optional[datetime] = None) -> str:
    dt = ts or datetime.now()
    if dt.weekday() >= 5:
        return "休市"
    t = dt.time()
    if t < dtime(9, 15):
        return "未开盘"
    if dtime(9, 15) <= t < dtime(9, 30):
        return "集合竞价"
    if dtime(9, 30) <= t <= dtime(11, 30):
        return "交易日中" if t < dtime(11, 30) else "上午收盘"
    if dtime(11, 30) < t < dtime(13, 0):
        return "午间休市"
    if dtime(13, 0) <= t < dtime(15, 0):
        return "交易中"
    if t == dtime(15, 0):
        return "已收盘"
    return "已收盘"


# --------------------------------------------------------------------------
# 提醒引擎
# --------------------------------------------------------------------------
@dataclass
class AlertEvent:
    secid: str
    name: str
    price: float
    prev_close: float
    reason: str
    detail: str
    ts: float = field(default_factory=time.time)

    @property
    def pct(self) -> float:
        if self.price and self.prev_close:
            return (self.price - self.prev_close) / self.prev_close * 100.0
        return 0.0


class AlertEngine:
    """边沿触发：穿越阈值时才提醒一次，价格回落后自动重新武装。"""

    def __init__(self, config: Config, log=None):
        self.config = config
        self.log = log or (lambda msg: None)
        self.last_fired: Dict[str, float] = {}

    def evaluate(self, quotes: Dict[str, Quote],
                 cooldown: float = 20.0) -> List[AlertEvent]:
        events: List[AlertEvent] = []
        now = time.time()
        for secid, quote in quotes.items():
            rule = self.config.alerts().get(secid)
            if not isinstance(rule, dict) or not rule.get("on", True):
                continue
            price = quote.price
            if not price:
                continue
            name = quote.display_name

            above = _opt_float(rule.get("above"))
            if above is not None:
                if price >= above and rule.get("_armed_above", True):
                    rule["_armed_above"] = False
                    events.append(AlertEvent(secid, name, price, quote.prev_close,
                                             "above", f"价格 {price:.2f} ≥ 上限 {above:.2f}"))
                elif price < above:
                    rule["_armed_above"] = True

            below = _opt_float(rule.get("below"))
            if below is not None:
                if price <= below and rule.get("_armed_below", True):
                    rule["_armed_below"] = False
                    events.append(AlertEvent(secid, name, price, quote.prev_close,
                                             "below", f"价格 {price:.2f} ≤ 下限 {below:.2f}"))
                elif price > below:
                    rule["_armed_below"] = True

            pct = _opt_float(rule.get("pct"))
            if pct is not None and quote.prev_close:
                cur_pct = quote.pct
                if abs(cur_pct) >= abs(pct) and rule.get("_armed_pct", True):
                    rule["_armed_pct"] = False
                    word = "涨幅" if pct > 0 else "跌幅"
                    events.append(AlertEvent(secid, name, price, quote.prev_close,
                                             "pct", f"{word} {cur_pct:+.2f}% 达到 {pct:+.2f}%"))
                elif abs(cur_pct) < abs(pct):
                    rule["_armed_pct"] = True

        fresh: List[AlertEvent] = []
        for ev in events:
            key = f"{ev.secid}:{ev.reason}"
            if now - self.last_fired.get(key, 0) < cooldown:
                continue
            self.last_fired[key] = now
            fresh.append(ev)
            self.log(f"提醒 {ev.name} {ev.detail}")
        if fresh:
            self.config.save()
        return fresh


def _opt_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def alert_text(rule: Dict[str, Any]) -> str:
    """把提醒规则渲染成简短文案。"""
    if not isinstance(rule, dict) or not rule.get("on", True):
        return ""
    bits = []
    above = _opt_float(rule.get("above"))
    below = _opt_float(rule.get("below"))
    pct = _opt_float(rule.get("pct"))
    if above is not None:
        bits.append(f"≥{above:g}")
    if below is not None:
        bits.append(f"≤{below:g}")
    if pct is not None:
        bits.append(f"{pct:+g}%")
    return " ".join(bits)


# --------------------------------------------------------------------------
# JSON 输出（供外部程序调用）
# --------------------------------------------------------------------------
def _json_quotes() -> int:
    """python stock_core.py --json 1.600519,0.300750 [--config <目录>]

    输出 UTF-8 JSON（便于 Windows 下无 BOM 读取）：
      {"ok": true, "source": "东财", "time": "...", "list": [ {...}, ... ]}
    """
    cfg_dir = None
    secids: List[str] = []
    argv = sys.argv[1:]
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in ("--json", "-"):
            idx += 1
            continue
        if arg in ("--config", "-c"):
            if idx + 1 < len(argv):
                cfg_dir = argv[idx + 1]
                idx += 2
                continue
            idx += 1
            continue
        if arg.startswith("--config="):
            cfg_dir = arg.split("=", 1)[1]
            idx += 1
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        secids.extend([p for p in re.split(r"[,\s]+", arg) if p])
        idx += 1

    if not secids and cfg_dir:
        secids = [s["secid"] for s in Config(os.path.join(cfg_dir, "stocks.json")).stocks()]
    if not secids:
        secids = [s["secid"] for s in DEFAULT_STOCKS]

    client = QuoteClient()
    quotes = client.fetch(secids)
    rows = []
    for secid in secids:
        quote = quotes.get(secid)
        if not quote:
            rows.append({"secid": secid, "ok": False})
            continue
        rows.append({
            "secid": secid,
            "ok": True,
            "code": quote.code,
            "name": quote.display_name,
            "price": round(quote.price, 4),
            "prev_close": round(quote.prev_close, 4),
            "change": round(quote.change, 4),
            "pct": round(quote.pct, 3),
            "high": round(quote.high, 4),
            "low": round(quote.low, 4),
            "open": round(quote.open, 4),
            "volume_ratio": round(quote.volume_ratio, 3),
            "turnover": round(quote.turnover, 3),
            "main_net": round(quote.main_net, 2),
            "main_net_pct": round(quote.main_net_pct, 3),
            "time": quote.quote_time,
            "live": quote.is_live,
        })
    payload = {
        "ok": bool(quotes),
        "source": client.last_source,
        "error": client.last_error,
        "phase": market_phase(),
        "trading": is_trading_time(),
        "time": datetime.now().strftime("%H:%M:%S"),
        "list": rows,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.write(data.decode("utf-8", "replace"))
    return 0 if quotes else 1


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
def _selftest() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"=== {APP_NAME} v{APP_VERSION} 自测 ===")
    print("交易时段:", is_trading_time(), "| 状态:", market_phase())

    # 读一下已保存的配置：这样自测能验证用户填的 Tushare token 是否真的可用
    try:
        saved = Config(os.path.join(_BASE_DIR, "stocks.json"))
        token = str(saved.data.get("tushare_token") or "").strip()
        if token:
            init_tushare(token, bool(saved.data.get("tushare_enabled", True)))
            print(f"已从 stocks.json 载入 Tushare token（尾 {token[-6:]}）")
    except Exception as exc:  # noqa: BLE001
        print("读取 stocks.json 失败:", exc)

    # Tushare 状态（有 token 就真的取一次数据验证）
    ts_state = tushare_status()
    print("\nTushare 状态:")
    print(f"  已安装 tushare 包: {ts_state['installed']}")
    print(f"  已配置 token     : {ts_state['token_configured']}")
    print(f"  可用             : {ts_state['api_ready']}  {ts_state['reason'] or ''}")
    if ts_state["api_ready"]:
        try:
            bars = _fetch_kline_tushare("1.600519", "daily", 1, 5)
            print(f"  Tushare 日K 测试 : {len(bars)} 根"
                  + (f" 最新 {bars[-1].date} 收 {bars[-1].close}" if bars else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"  Tushare 日K 测试 : FAIL {type(exc).__name__} {exc}")
        try:
            flows = _fetch_fundflow_tushare("1.600519", 5)
            print(f"  Tushare 资金流   : {len(flows)} 条"
                  + (f" 最新 {flows[-1].date} 主力 {flows[-1].main_net / 1e4:+.0f}万"
                     if flows else "（可能积分不足）"))
        except Exception as exc:  # noqa: BLE001
            print(f"  Tushare 资金流   : FAIL {type(exc).__name__} {exc}")

    client = QuoteClient()
    secids = ["1.600519", "0.300750", "0.000001", "1.000001", "116.00700", "105.AAPL"]
    quotes = client.fetch(secids)
    print(f"行情来源: {client.last_source or '无'} | 返回 {len(quotes)}/{len(secids)} 条")
    for secid in secids:
        q = quotes.get(secid)
        if q:
            print(f"  {secid:<10} {q.display_name:<10} {q.price:>10.2f} "
                  f"{q.change:+.2f} ({q.pct:+.2f}%) 昨收 {q.prev_close:.2f} 量比 {q.volume_ratio:.2f}")
        else:
            print(f"  {secid:<10} 未获取到")
    if not quotes:
        print("!! 行情接口不可用")
        return 1

    print("\n搜索测试:")
    for kw in ("茅台", "600519", "腾讯", "AAPL"):
        results = client.search(kw)
        short = ", ".join(f"{r['name']}({r['secid']})" for r in results[:4]) or "无结果"
        print(f"  {kw:<8} -> {short}")

    print("\n代码解析测试:")
    for raw in ("600519", "sh600519", "600519.SH", "00700", "hk00700", "AAPL", "300750.SZ", "??"):
        print(f"  {raw:<12} -> {parse_symbol(raw)}")

    print("\n资金流测试（当日主力净流入）:")
    for secid in secids[:4]:
        q = quotes.get(secid)
        if q:
            print(f"  {q.display_name:<10} 主力 {q.main_net / 1e4:>+12.0f}万 ({q.main_net_pct:+.2f}%) | "
                  f"超大单 {q.super_net / 1e4:>+10.0f}万 | 大单 {q.big_net / 1e4:>+10.0f}万 | "
                  f"小单 {q.small_net / 1e4:>+10.0f}万")
    detail = fetch_fundflow_detail("1.600519")
    print(f"  明细接口: {detail.name} 主力 {detail.main_net / 1e4:+.0f}万 占比 {detail.main_net_pct:+.2f}% "
          f"(超大 {detail.super_net / 1e4:+.0f}万 / 大 {detail.big_net / 1e4:+.0f}万 / "
          f"中 {detail.mid_net / 1e4:+.0f}万 / 小 {detail.small_net / 1e4:+.0f}万)")

    print("\nK 线测试:")
    minute_times = {}
    for period in ("daily", "5min", "1min"):
        chart = fetch_kline("1.600519", period=period, adjust=1, limit=30)
        if chart.bars:
            last = chart.bars[-1]
            ma5 = chart.ma(5)[-1]
            if period.endswith("min"):
                minute_times[period] = [b.date for b in chart.bars[-3:]]
            print(f"  {KLT_LABEL.get(period, period):<4} {chart.name} {len(chart.bars)} 根 | 最新 {last.date} "
                  f"开{last.open:.2f} 高{last.high:.2f} 低{last.low:.2f} 收{last.close:.2f} "
                  f"({last.pct:+.2f}%) 量{last.volume:.0f}手 MA5={ma5:.2f} 源={chart.source}"
                  if ma5 else "")
        else:
            print(f"  {period} 无数据")
    if "1min" in minute_times and "5min" in minute_times:
        same = minute_times["1min"] == minute_times["5min"]
        print(f"  分时(1min) 与 5分 是否相同: {'相同（异常）' if same else '不同（正常）'}")
        print(f"    1min 末三根: {minute_times['1min']}")
        print(f"    5min 末三根: {minute_times['5min']}")

    print("\n数据源熔断粒度测试:")
    kline_key = _cooldown_key(EM_KLINE_URL + "?secid=1.600519")
    fflow_key = _cooldown_key(EM_FFLOW_URL + "?secid=1.600519")
    print(f"  kline 冷却键: {kline_key}")
    print(f"  fflow 冷却键: {fflow_key}")
    print(f"  两者独立（不会互相连坐）: {kline_key != fflow_key}")

    print("\n资金流历史测试:")
    try:
        flows = fetch_fundflow_history("1.600519", limit=6)
    except Exception as exc:  # noqa: BLE001
        flows = []
        print(f"  !! 资金流历史不可用（{type(exc).__name__}），通常是接口限流，稍后自动恢复")
    for bar in flows:
        print(f"  {bar.date}  收 {bar.close:>8.2f} ({bar.pct:+.2f}%)  "
              f"主力 {bar.main_net / 1e4:>+12.0f}万  超大 {bar.super_net / 1e4:>+12.0f}万  "
              f"大单 {bar.big_net / 1e4:>+12.0f}万  小单 {bar.small_net / 1e4:>+12.0f}万")

    print("\nETF 支持测试:")
    etfs = [("1.510300", "沪深300ETF"), ("0.159915", "创业板ETF"), ("1.588000", "科创50ETF"),
            ("1.518880", "黄金ETF"), ("0.159941", "纳指ETF")]
    etf_quotes = client.fetch([s for s, _ in etfs])
    for secid, label in etfs:
        quote = etf_quotes.get(secid)
        if quote:
            flow = f"{quote.main_net / 1e4:+.0f}万" if quote.main_net else "--(ETF无资金流)"
            print(f"  {label:<10} {quote.display_name:<16} {quote.price:>8.3f} {quote.pct:+.2f}%  "
                  f"主力 {flow}")
        else:
            print(f"  {label:<10} 未获取到")
    try:
        etf_chart = fetch_kline("1.510300", period="daily", limit=10)
        print(f"  ETF 日K: {etf_chart.name} {len(etf_chart.bars)} 根，来源 {etf_chart.source}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ETF 日K 不可用: {type(exc).__name__}")

    print("\n提醒引擎测试:")
    import os as _os
    import tempfile
    cfg = Config(_os.path.join(tempfile.gettempdir(), "stock_selftest_cfg.json"))
    sample = quotes.get("1.600519")
    if sample:
        rule = cfg.alert_for("1.600519")
        rule["above"] = round(sample.price - 1, 2)   # 必然触发
        rule["below"] = round(sample.price - 2, 2)   # 必然触发
        rule["pct"] = 0.01                           # 必然触发
        rule["_armed_above"] = rule["_armed_below"] = rule["_armed_pct"] = True
        engine = AlertEngine(cfg, log=print)
        first = engine.evaluate(quotes)
        second = engine.evaluate(quotes)
        print(f"  第一次触发 {len(first)} 条（预期 3），第二次触发 {len(second)} 条（预期 0，防重复）")
        for ev in first:
            print(f"    · {ev.name} {ev.detail}  [{ev.pct:+.2f}%]")
        rule["above"] = round(sample.price + 100, 2)
        rule["below"] = round(sample.price - 100, 2)
        rule["pct"] = 99.0
        engine.evaluate(quotes)  # 让它重新武装
        ok = len(first) == 3 and len(second) == 0
        print("  提醒逻辑:", "通过" if ok else "异常")
    print("\n自测结束。")
    return 0


# --------------------------------------------------------------------------
# ETF 持仓（天天基金 F10 / 东方财富）
#
# 数据源：https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc
# 返回的是一段 JS：var apidata={ content:"<div>...<table>..." ,arryear:[...]}
#   · 不带 year/month       -> 第一张表 = 最新报告期的「前十大」；
#   · 带 year=YYYY&month=M  -> 第一张表 = 该报告期的「全部持仓」（年报/半年报口径）。
#
# 两个实测坑：
#   1) 响应里通常跟着好几个季度的小表（前十大），**只能取第一张表**；
#      若把所有 <tr> 都算上，前十大测试会变成 30 行、全量会虚高 30 行。
#   2) 「前十大」表的行是 9 列（权重在下标 6），「全量」表只有 7 列（权重在下标 4），
#      所以权重列不能写死，要先从表头「占净值比例」定位。
# --------------------------------------------------------------------------
JJCC_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
JJCC_REFERER = "https://fund.eastmoney.com/"
ETF_HOLDINGS_TTL = 6 * 3600          # 持仓按季度披露，缓存 6 小时足够

_TAG_RE = re.compile(r"<[^>]+>")
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_PERIOD_RE = re.compile(r"(\d{4}年\d{1,2}季度)股票投资明细")
_ANCHOR_TITLE_RE = re.compile(r"<a\s[^>]*title=(['\"])(.*?)\1", re.S | re.I)
_CODE6_RE = re.compile(r"^\d{6}$")


@dataclass
class EtfHolding:
    """ETF 的一只成分股持仓。"""
    code: str            # 6 位股票代码
    name: str = ""
    weight: float = 0.0  # 占净值比例 %，例如 3.13


@dataclass
class EtfHoldings:
    """某只 ETF 在某个报告期的持仓快照。"""
    etf_code: str
    etf_name: str = ""
    period: str = ""            # 例如 "2026年3季度"
    kind: str = "top10"         # "top10" | "full"
    items: List[EtfHolding] = field(default_factory=list)
    total_weight: float = 0.0   # items 的权重之和
    source: str = "天天基金"
    updated_at: str = ""        # 抓取时间 HH:MM:SS
    error: str = ""             # 失败原因（为空表示成功）


def _clean_cell(raw: Any) -> str:
    """剥掉 HTML 标签 / &nbsp; / 多余空白，得到纯文本单元格。"""
    text = _TAG_RE.sub("", str(raw if raw is not None else ""))
    for ent, rep in (("&nbsp;", " "), ("&#160;", " "), ("\xa0", " "),
                     ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(ent, rep)
    return re.sub(r"\s+", " ", text).strip()


def _parse_weight(raw: Any) -> Optional[float]:
    """把 "3.13%" / " 2.73 % " / "1,234.5%" 解析成 float；解不出返回 None。"""
    text = _clean_cell(raw)
    if not text or text in ("-", "--", "—", "－", "N/A", "n/a"):
        return None
    text = text.replace("%", "").replace(",", "").replace(" ", "")
    if text.startswith("+"):
        text = text[1:]
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return value


def _parse_jjcc(text: Any) -> Tuple[str, str, List[EtfHolding]]:
    """纯函数：解析天天基金 jjcc 接口返回的 JS/HTML。

    只解析**第一张表**（= 该响应对应的报告期），返回：
        (报告期如 "2026年3季度", ETF 名称, 去重并按权重降序的持仓列表)

    容错：剥标签、去 &nbsp;、从表头定位「占净值比例」列；
    表头 / 合计 / 债券（权重为 "--"）等非持仓行跳过；
    同一代码重复出现时保留较大权重；权重超出 (0, 100] 的行跳过。
    不联网、不抛异常。
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    text = str(text or "")
    if not text:
        return "", "", []

    period = ""
    match = _PERIOD_RE.search(text)
    if match:
        period = match.group(1)

    # 第一个带 title 的 <a> 就是页头的 ETF 名称（持仓行里的 <a> 没有 title）
    etf_name = ""
    match = _ANCHOR_TITLE_RE.search(text)
    if match:
        etf_name = _clean_cell(match.group(2))
    if not etf_name:
        match = re.search(r"<h4[^>]*>.*?<a[^>]*>([^<]+)</a>", text, re.S | re.I)
        if match:
            etf_name = _clean_cell(match.group(1))

    tables = _TABLE_RE.findall(text)
    body = tables[0] if tables else text

    rows: List[List[str]] = []
    weight_col = -1
    for row_html in _TR_RE.findall(body):
        cells = [_clean_cell(c) for c in _CELL_RE.findall(row_html)]
        if not any(cells):
            continue
        if weight_col < 0:
            for idx, cell in enumerate(cells):
                if "占净值" in cell:          # 表头：占净值<br/>比例 -> "占净值 比例"
                    weight_col = idx
                    break
        rows.append(cells)

    found: Dict[str, EtfHolding] = {}
    for cells in rows:
        if len(cells) < 4:
            continue
        code = cells[1]
        if not _CODE6_RE.match(code or ""):
            continue          # 表头（"股票代码"）/ 合计行（无代码）等

        weight: Optional[float] = None
        if 0 <= weight_col < len(cells):
            weight = _parse_weight(cells[weight_col])
        else:
            # 表头没定位到时按已知结构兜底：9 列 -> 下标 6，7 列 -> 下标 4
            fixed = 6 if len(cells) >= 9 else (4 if len(cells) >= 7 else -1)
            if 0 <= fixed < len(cells):
                weight = _parse_weight(cells[fixed])
            if weight is None:
                for cell in cells[3:]:
                    if "%" in cell:
                        weight = _parse_weight(cell)
                        if weight is not None:
                            break
        if weight is None or not (0.0 <= weight <= 100.0):
            continue          # 债券行（"--"）等

        name = cells[2] if len(cells) > 2 else ""
        old = found.get(code)
        if old is None or weight > old.weight:      # 去重：保留较大权重
            found[code] = EtfHolding(code=code, name=name, weight=round(weight, 4))

    items = sorted(found.values(), key=lambda h: (-h.weight, h.code))
    return period, etf_name, items


def is_etf_code(code: str) -> bool:
    """6 位代码是否像场内 ETF：沪市 51/56/58 开头，深市 15/16 开头。"""
    text = str(code or "").strip()
    if not _CODE6_RE.match(text):
        return False
    return text.startswith(("51", "56", "58", "15", "16"))


def etf_secid(code: str) -> str:
    """6 位代码 -> 东财 secid。

    沪市（5/6/9 开头）-> "1.xxxxxx"；深市/北交（0/1/2/3/4/8 开头）-> "0.xxxxxx"。
    不是 6 位数字时返回空串。
    """
    text = str(code or "").strip()
    if not _CODE6_RE.match(text):
        return ""
    return ("1." if text[0] in "569" else "0.") + text


def _jjcc_url(code: str, year: Any = "", month: Any = "") -> str:
    """拼 jjcc 接口 URL；year/month 为空时取最新报告期。"""
    params = {
        "type": "jjcc",
        "code": code,
        "topline": "10",
        "year": "" if year in ("", None) else str(year),
        "month": "" if month in ("", None) else str(month),
    }
    return JJCC_URL + "?" + urllib.parse.urlencode(params)


def _etf_from_cache(data: Any) -> Optional[EtfHoldings]:
    """把 CACHE 里的 dict 还原成 EtfHoldings；结构不对返回 None。"""
    if not isinstance(data, dict):
        return None
    try:
        items: List[EtfHolding] = []
        for row in data.get("items") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if not _CODE6_RE.match(code):
                continue
            items.append(EtfHolding(
                code=code,
                name=str(row.get("name") or ""),
                weight=_fnum(row.get("weight")),
            ))
        return EtfHoldings(
            etf_code=str(data.get("etf_code") or ""),
            etf_name=str(data.get("etf_name") or ""),
            period=str(data.get("period") or ""),
            kind=str(data.get("kind") or "top10"),
            items=items,
            total_weight=_fnum(data.get("total_weight"), round(sum(h.weight for h in items), 2)),
            source=str(data.get("source") or "天天基金"),
            updated_at=str(data.get("updated_at") or ""),
        )
    except Exception:      # noqa: BLE001 - 缓存坏了就当没命中
        return None


def _etf_log(msg: str) -> None:
    """ETF 持仓抓取的简单日志；写失败也不影响调用方。"""
    try:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [ETF持仓] {msg}"
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "stock_monitor.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:      # noqa: BLE001
        pass


def fetch_etf_holdings(etf_code: str, full: bool = False, timeout: float = 12.0) -> EtfHoldings:
    """抓 ETF 持仓（失败不抛异常，返回带 error 的空 EtfHoldings）。

    full=False: 当前报告期「前十大」，kind="top10"（按季度披露，较新）。
    full=True : 尽可能完整的持仓，kind="full"（年报/半年报口径，较旧但完整；
                用年份参数 year=<上一年>&month=12 取上一份年报，必要时退回半年报）。
    """
    code = str(etf_code or "").strip()
    kind = "full" if full else "top10"
    now = datetime.now().strftime("%H:%M:%S")
    result = EtfHoldings(etf_code=code, kind=kind, updated_at=now, source="天天基金")
    if not _CODE6_RE.match(code):
        result.error = "ETF 代码需为 6 位数字"
        return result

    cache_key = f"etf_holdings_{code}_{kind}"
    ttl = CACHE_TTL.get("etf_holdings", ETF_HOLDINGS_TTL)
    hit = CACHE.get(cache_key, ttl=ttl)
    if hit:
        cached = _etf_from_cache(hit[0] if isinstance(hit, tuple) else hit)
        if cached is not None:
            cached.kind = kind
            return cached

    last_exc: Optional[Exception] = None
    if full:
        # 上一份年报最全；1~4 月年报还没披露时，退回上一年的半年报。
        year = datetime.now().year
        best: Optional[Tuple[str, str, List[EtfHolding]]] = None
        for yy, mm in ((year - 1, 12), (year - 1, 6), (year - 2, 12)):
            try:
                raw = _http_get(_jjcc_url(code, yy, mm), referer=JJCC_REFERER, timeout=timeout)
            except Exception as exc:      # noqa: BLE001
                last_exc = exc
                continue
            parsed = _parse_jjcc(raw.decode("utf-8", "replace"))
            if best is None or len(parsed[2]) > len(best[2]):
                best = parsed
            if len(parsed[2]) >= 50:      # 已经拿到"全部持仓"量级，够用了
                break
        if best is None:
            result.error = f"抓取失败: {type(last_exc).__name__}: {last_exc}" if last_exc else "抓取失败"
            _etf_log(f"{code} {kind}: {result.error}")
            return result
        period, etf_name, items = best
    else:
        try:
            raw = _http_get(_jjcc_url(code), referer=JJCC_REFERER, timeout=timeout)
        except Exception as exc:          # noqa: BLE001
            result.error = f"抓取失败: {type(exc).__name__}: {exc}"
            _etf_log(f"{code} {kind}: {result.error}")
            return result
        period, etf_name, items = _parse_jjcc(raw.decode("utf-8", "replace"))

    result.etf_name = etf_name
    result.period = period
    result.items = items
    result.total_weight = round(sum(h.weight for h in items), 2)
    if not items:
        result.error = "未解析到持仓（报告期可能未披露，或接口结构变化）"
        _etf_log(f"{code} {kind}: {result.error} period={period!r}")
        return result

    result.updated_at = now
    CACHE.put(cache_key, {
        "etf_code": code,
        "etf_name": etf_name,
        "period": period,
        "kind": kind,
        "items": [{"code": h.code, "name": h.name, "weight": h.weight} for h in items],
        "total_weight": result.total_weight,
        "source": result.source,
        "updated_at": now,
    })
    return result


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or "--test" in sys.argv:
        raise SystemExit(_selftest())
    if "--json" in sys.argv:
        raise SystemExit(_json_quotes())
    print("这是核心模块，请运行 stock_app.py 启动界面。")
