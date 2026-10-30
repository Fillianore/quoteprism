# -*- coding: utf-8 -*-
"""股票监控 —— 本地网页版后端（纯标准库，零第三方依赖）。

把 `stock_core.py` 的数据能力包一层 HTTP JSON 接口，供浏览器里的前端调用：

    python web_server.py                  # 默认 http://127.0.0.1:8770/
    python web_server.py --port 8780
    python web_server.py --open           # 启动后自动打开默认浏览器
    python web_server.py --config <目录>  # 数据目录（stocks.json / cache），与桌面程序一致

设计要点
--------
* `ThreadingHTTPServer`：浏览器会同时发多个请求，必须并发处理；
* 所有 `/api/*` 一律 HTTP 200 + JSON，业务失败用 `{"ok": false, "error": "中文原因"}`；
* `/api/quotes` 对同一组 secid 缓存 2 秒；`/api/holdings` 的成分股行情用
  `fetch_quotes_sina()` 一次拿全并缓存 3 秒（543 只实测 ~0.3s）；
* 其它数据源（K 线 / 分时 / 资金流 / 持仓）本身在 stock_core 里有磁盘缓存，直接调用；
* 任何数据源异常只影响当次响应，绝不让进程崩、请求挂死；
* 静态资源只暴露 `web/index.html`、`web/app.js`、`web/style.css`（名字白名单 + 防穿越）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stock_core as C  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8770
PORT_TRIES = 10                       # 端口被占用时最多往后试 10 个
WEB_DIR = os.path.join(ROOT, "web")

QUOTES_TTL = 2.0                      # /api/quotes 内存缓存（秒）
HOLDINGS_QUOTES_TTL = 3.0             # 持仓成分股行情内存缓存（秒）
MAX_QUOTES = 300                      # 一次最多查询多少只（防御性上限）

# 前端允许的周期（与东财 klt 一一对应，键正好就是契约里允许的 8 种）
ALLOWED_PERIODS: Tuple[str, ...] = tuple(C.KLT_CHOICES.keys())

# 静态资源白名单：名字 -> (磁盘文件, Content-Type)
STATIC_FILES: Dict[str, Tuple[str, str]] = {
    "app.js": ("app.js", "application/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
class ApiError(Exception):
    """业务失败：HTTP 仍返回 200，body 为 {"ok": false, "error": "中文原因"}。"""


def _r(value: Any, digits: int = 4) -> float:
    """把任意值收敛成有限 float 并四舍五入（NaN/inf/异常 -> 0.0，避免 JSON 非法）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num or num in (float("inf"), float("-inf")):
        return 0.0
    out = round(num, digits)
    return 0.0 if out == 0 else out      # 顺手把 -0.0 变成 0.0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any, default: float) -> float:
    """配置里的刷新间隔等：整数值返回 int，否则保留小数。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num or num in (float("inf"), float("-inf")):
        return default
    return int(num) if num == int(num) else round(num, 3)


def _to_int(text: Any, default: int, low: Optional[int] = None,
            high: Optional[int] = None) -> int:
    try:
        num = int(str(text).strip())
    except (TypeError, ValueError):
        num = default
    if low is not None:
        num = max(low, num)
    if high is not None:
        num = min(high, num)
    return num


def _param(query: Dict[str, List[str]], name: str, default: str = "") -> str:
    values = query.get(name) or []
    if not values:
        return default
    value = values[0]
    return default if value is None else str(value)


def _hhmm(date_text: Any) -> str:
    """'2026-10-29 09:31:00' -> '09:31'（拿不到就空串）。"""
    text = str(date_text or "")
    if " " not in text:
        return ""
    return text.split(" ", 1)[1][:5]


def _minute_index(date_text: Any) -> Optional[int]:
    """把一根分钟线映射到「当日第几分钟」：0=09:30，120=11:30/13:00，240=15:00。

    午休不占长度，缺数据时天然留缺口 —— 与桌面程序 `tick_minute_of` 口径一致。
    """
    text = str(date_text or "")
    if " " not in text:
        return None
    try:
        hh, mm = (int(part) for part in text.split(" ", 1)[1][:5].split(":"))
    except (TypeError, ValueError):
        return None
    cur = hh * 60 + mm
    am_open, am_close = 9 * 60 + 30, 11 * 60 + 30
    pm_open, pm_close = 13 * 60, 15 * 60
    if cur < am_open:
        return 0
    if cur <= am_close:
        return cur - am_open
    if cur < pm_open:
        return 120
    if cur <= pm_close:
        return 120 + (cur - pm_open)
    return 240


def _quote_to_item(quote: Any, secid: str, fallback_name: str = "") -> Dict[str, Any]:
    """stock_core.Quote -> 契约里的报价对象。"""
    code = _text(getattr(quote, "code", "")) or (secid.split(".", 1)[1] if "." in secid else secid)
    name = _text(getattr(quote, "name", "")) or fallback_name
    try:
        live = bool(quote.is_live)
    except Exception:                    # noqa: BLE001 - 属性异常也不该影响接口
        live = False
    return {
        "secid": secid,
        "code": code,
        "name": name,
        "price": _r(getattr(quote, "price", 0.0)),
        "prev_close": _r(getattr(quote, "prev_close", 0.0)),
        "change": _r(getattr(quote, "change", 0.0)),
        "pct": _r(getattr(quote, "pct", 0.0)),
        "open": _r(getattr(quote, "open", 0.0)),
        "high": _r(getattr(quote, "high", 0.0)),
        "low": _r(getattr(quote, "low", 0.0)),
        "volume_ratio": _r(getattr(quote, "volume_ratio", 0.0), 3),
        "turnover": _r(getattr(quote, "turnover", 0.0), 3),
        "main_net": _r(getattr(quote, "main_net", 0.0), 2),
        "main_net_pct": _r(getattr(quote, "main_net_pct", 0.0), 2),
        "quote_time": _text(getattr(quote, "quote_time", "")),
        "live": live,
        "ok": True,
    }


def _empty_quote_item(secid: str, fallback_name: str = "") -> Dict[str, Any]:
    """请求了但数据源没给这只（停牌/代码错误/断网）：给占位项，前端能继续画行。"""
    code = secid.split(".", 1)[1] if "." in secid else secid
    return {
        "secid": secid, "code": code, "name": fallback_name,
        "price": 0.0, "prev_close": 0.0, "change": 0.0, "pct": 0.0,
        "open": 0.0, "high": 0.0, "low": 0.0,
        "volume_ratio": 0.0, "turnover": 0.0,
        "main_net": 0.0, "main_net_pct": 0.0,
        "quote_time": "", "live": False, "ok": False,
    }


class _TTLCache:
    """极简线程安全 TTL 缓存（只存最近若干组，避免长时间运行无限增长）。"""

    def __init__(self, ttl: float, max_entries: int = 256) -> None:
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._data: Dict[Any, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stamp, value = entry
            if self.ttl > 0 and (time.time() - stamp) > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            if len(self._data) > self.max_entries:
                ordered = sorted(self._data.items(), key=lambda kv: kv[1][0])
                for old_key, _ in ordered[: len(ordered) - self.max_entries]:
                    self._data.pop(old_key, None)


def _parse_config_dir(argv: Optional[List[str]] = None) -> str:
    """与桌面程序一致的 `--config <目录>` 解析（目录，不是文件）。"""
    args = list(sys.argv if argv is None else argv)
    for idx, arg in enumerate(args):
        candidate = ""
        if arg in ("--config", "-c") and idx + 1 < len(args):
            candidate = args[idx + 1]
        elif arg.startswith("--config="):
            candidate = arg.split("=", 1)[1]
        if candidate:
            try:
                return os.path.abspath(os.path.expanduser(candidate))
            except Exception:            # noqa: BLE001
                break
    return ROOT


def default_config_path(argv: Optional[List[str]] = None) -> str:
    return os.path.join(_parse_config_dir(argv), "stocks.json")


# --------------------------------------------------------------------------
# 网页版业务对象（配置 + 内存缓存）
# --------------------------------------------------------------------------
class WebApp:
    """把 `Config` 与 stock_core 的取数函数组合成一组 JSON 友好的方法。"""

    def __init__(self, config_path: str) -> None:
        self.config_path = os.path.abspath(str(config_path))
        self.config = C.Config(self.config_path)
        self._lock = threading.RLock()          # 保护 config.data（并发读写）
        self._quotes = _TTLCache(QUOTES_TTL)
        self._holdings_quotes = _TTLCache(HOLDINGS_QUOTES_TTL)
        self._names: Dict[str, str] = {}        # secid -> 名称（进程内长期）
        self._flows: Dict[str, Tuple[List[Dict[str, Any]], str]] = {}   # 资金流失败兜底
        self.started_at = time.time()

    # ---- 自选 ----------------------------------------------------------
    def _stocks(self) -> List[Dict[str, str]]:
        with self._lock:
            return self.config.stocks()

    def _stocks_for_api(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for row in self._stocks():
            secid = _text(row.get("secid"))
            if not secid:
                continue
            code = _text(row.get("code")) or (secid.split(".", 1)[1] if "." in secid else secid)
            out.append({"secid": secid, "code": code, "name": _text(row.get("name"))})
        return out

    def watchlist_payload(self) -> Dict[str, Any]:
        return {"ok": True, "list": self._stocks_for_api()}

    # ---- 名称 ----------------------------------------------------------
    def _remember_name(self, secid: str, name: str) -> str:
        name = _text(name)
        if name:
            with self._lock:
                self._names[secid] = name
        return name

    def resolve_name(self, secid: str, hint: str = "") -> str:
        """优先用调用方给的名字；其次自选里的名字；最后问一次行情源（失败就算了）。"""
        if hint:
            return self._remember_name(secid, hint)
        with self._lock:
            cached = self._names.get(secid)
        if cached:
            return cached
        for row in self._stocks():
            if row["secid"] == secid and row.get("name"):
                return self._remember_name(secid, row["name"])
        try:
            items = self._quote_items([secid])[0]
            name = _text(items.get(secid, {}).get("name"))
        except Exception:                # noqa: BLE001 - 名称只是装饰，不能影响接口
            name = ""
        return self._remember_name(secid, name)

    def _quote_name(self, secid: str) -> str:
        try:
            return _text(self._quote_items([secid])[0].get(secid, {}).get("name"))
        except Exception:                # noqa: BLE001
            return ""

    # ---- 行情（2 秒缓存）-----------------------------------------------
    def _quote_items(self, secids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], str, str]:
        """返回 (secid -> 报价对象, source, error)；同一组 secid 2 秒内复用。"""
        wanted = list(dict.fromkeys(secid for secid in secids if secid))
        key = tuple(sorted(wanted))
        cached = self._quotes.get(key)
        if cached is not None:
            return cached

        client = C.QuoteClient()
        try:
            raw = client.fetch(wanted)
            source = _text(getattr(client, "last_source", ""))
            error = _text(getattr(client, "last_error", ""))
        except Exception as exc:          # noqa: BLE001 - 取数异常只影响本次响应
            raw, source, error = {}, "", f"{type(exc).__name__}: {exc}"

        fallback_names = {row["secid"]: _text(row.get("name")) for row in self._stocks()}
        items: Dict[str, Dict[str, Any]] = {}
        for secid in wanted:
            quote = raw.get(secid)
            if quote is None:
                items[secid] = _empty_quote_item(secid, fallback_names.get(secid, ""))
            else:
                item = _quote_to_item(quote, secid, fallback_names.get(secid, ""))
                items[secid] = item
                self._remember_name(secid, item["name"])

        result = (items, source, error)
        self._quotes.put(key, result)
        return result

    def quotes_payload(self, raw_secids: str) -> Dict[str, Any]:
        secids = [part.strip() for part in _text(raw_secids).split(",") if part.strip()]
        if not secids:                    # 省略 / 空 -> 跟随当前自选
            secids = [row["secid"] for row in self._stocks()]
        secids = list(dict.fromkeys(secids))
        if not secids:
            return {"ok": True, "source": "", "error": "", "list": []}
        if len(secids) > MAX_QUOTES:
            raise ApiError(f"一次最多查询 {MAX_QUOTES} 只证券")
        items, source, error = self._quote_items(secids)
        return {"ok": True, "source": source, "error": error,
                "list": [items[secid] for secid in secids]}

    # ---- 通货（贵金属/原油/外汇）--------------------------------------
    def commodities_payload(self) -> Dict[str, Any]:
        with self._lock:
            raw_items = self.config.data.get("commodities") or []
            metas = [dict(item) for item in raw_items
                     if isinstance(item, dict) and item.get("code")]
        try:
            data = C.fetch_commodities(metas)
        except Exception:                # noqa: BLE001 - 期货/外汇挂了也要返回占位
            data = {}
        rows: List[Dict[str, Any]] = []
        for meta in metas:
            code = _text(meta.get("code"))
            commodity = data.get(code) if data else None
            if commodity is None:
                rows.append({
                    "code": code, "name": _text(meta.get("name")),
                    "price": 0.0, "prev_close": 0.0, "change": 0.0, "pct": 0.0,
                    "unit": _text(meta.get("unit")), "stale": True,
                    "quote_time": "", "source": "",
                })
                continue
            rows.append({
                "code": code,
                "name": _text(getattr(commodity, "name", "")) or _text(meta.get("name")),
                "price": _r(getattr(commodity, "price", 0.0)),
                "prev_close": _r(getattr(commodity, "prev_close", 0.0)),
                "change": _r(getattr(commodity, "change", 0.0)),
                "pct": _r(getattr(commodity, "pct", 0.0)),
                "unit": _text(getattr(commodity, "unit", "")) or _text(meta.get("unit")),
                "stale": bool(getattr(commodity, "stale", False)),
                "quote_time": _text(getattr(commodity, "quote_time", "")),
                "source": _text(getattr(commodity, "source", "")),
            })
        return {"ok": True, "list": rows}

    # ---- K 线 ----------------------------------------------------------
    def kline_payload(self, secid: str, period: str, adjust: int, limit: int) -> Dict[str, Any]:
        secid = _text(secid).strip()
        if not secid:
            raise ApiError("缺少 secid 参数")
        period = _text(period).strip() or "daily"
        if period not in ALLOWED_PERIODS:
            raise ApiError(f"不支持的周期: {period}（可用: {'/'.join(ALLOWED_PERIODS)}）")
        try:
            chart = C.fetch_kline(secid, period=period, adjust=adjust, limit=limit)
        except Exception as exc:         # noqa: BLE001
            raise ApiError(f"K线获取失败: {type(exc).__name__}: {exc}"[:200]) from exc

        bars: List[Dict[str, Any]] = [{
            "date": _text(bar.date),
            "open": _r(bar.open),
            "high": _r(bar.high),
            "low": _r(bar.low),
            "close": _r(bar.close),
            "volume": _r(bar.volume, 2),
            "amount": _r(bar.amount, 2),
            "pct": _r(bar.pct),
        } for bar in chart.bars]
        ma5 = [None if value is None else _r(value) for value in chart.ma(5)]
        ma20 = [None if value is None else _r(value) for value in chart.ma(20)]
        name = _text(getattr(chart, "name", "")) or self.resolve_name(secid)
        return {
            "ok": True,
            "secid": secid,
            "name": name,
            "period": period,
            "period_label": C.KLT_LABEL.get(period, period),
            "source": _text(getattr(chart, "source", "")),
            "bars": bars,
            "ma5": ma5,
            "ma20": ma20,
        }

    # ---- 当日分时 ------------------------------------------------------
    def tick_payload(self, secid: str) -> Dict[str, Any]:
        secid = _text(secid).strip()
        if not secid:
            raise ApiError("缺少 secid 参数")
        try:
            chart = C.fetch_tick_day(secid)
        except Exception as exc:         # noqa: BLE001
            raise ApiError(f"分时获取失败: {type(exc).__name__}: {exc}"[:200]) from exc

        # 昨收/名称走 2 秒行情缓存，避免重复请求
        quote_items = self._quote_items([secid])[0]
        quote = quote_items.get(secid) or {}
        prev_close = float(quote.get("prev_close") or 0.0)
        name = (_text(getattr(chart, "name", "")) or _text(quote.get("name"))
                or self.resolve_name(secid))

        bars: List[Dict[str, Any]] = []
        minutes = set()
        first_day = ""
        cum_amount = cum_volume = 0.0
        for bar in chart.bars:
            minute = _minute_index(bar.date)
            if minute is None:
                continue
            day = str(bar.date)[:10]
            if not first_day:
                first_day = day
            volume = float(getattr(bar, "volume", 0.0) or 0.0)
            amount = float(getattr(bar, "amount", 0.0) or 0.0)
            if not amount:               # 新浪分钟线不给成交额，用典型价估算（与桌面端一致）
                amount = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0 * volume * 100.0
            cum_amount += amount
            cum_volume += volume
            avg = (cum_amount / cum_volume / 100.0) if cum_volume > 0 else float(bar.close)
            bars.append({
                "t": _hhmm(bar.date),
                "minute": minute,
                "close": _r(bar.close),
                "avg": _r(avg),
                "volume": _r(volume, 2),
            })
            minutes.add(minute)

        flow: List[Dict[str, Any]] = []
        try:
            intraday = C.fetch_fundflow_intraday(secid)
        except Exception:                # noqa: BLE001 - 资金流拿不到就给 []，契约允许
            intraday = []
        for bar in intraday or []:
            minute = _minute_index(getattr(bar, "date", ""))
            if minute is None:
                continue
            flow.append({"t": _hhmm(bar.date), "minute": minute, "main": _r(bar.main_net, 2)})

        today = time.strftime("%Y-%m-%d")
        date = first_day or today
        return {
            "ok": True,
            "secid": secid,
            "name": name,
            "date": date,
            "same_day": date == today,
            "prev_close": _r(prev_close),
            "coverage": len(minutes),
            "bars": bars,
            "flow": flow,
        }

    # ---- 历史资金流 ----------------------------------------------------
    def fundflow_payload(self, secid: str, limit: int) -> Dict[str, Any]:
        secid = _text(secid).strip()
        if not secid:
            raise ApiError("缺少 secid 参数")
        try:
            bars = C.fetch_fundflow_history(secid, limit=limit)
        except Exception as exc:         # noqa: BLE001 - 失败退到上次成功的内存数据
            with self._lock:
                cached = self._flows.get(secid)
            if cached:
                items, stamp = cached
                return {"ok": True, "stale": True, "time": stamp, "list": list(items)}
            raise ApiError(f"资金流获取失败: {type(exc).__name__}: {exc}"[:200]) from exc

        items: List[Dict[str, Any]] = [{
            "date": _text(bar.date),
            "close": _r(bar.close),
            "pct": _r(bar.pct),
            "main_net": _r(bar.main_net, 2),
            "super_net": _r(bar.super_net, 2),
            "big_net": _r(bar.big_net, 2),
            "mid_net": _r(bar.mid_net, 2),
            "small_net": _r(bar.small_net, 2),
        } for bar in (bars or [])][-limit:]
        stamp = time.strftime("%H:%M:%S")
        if items:
            with self._lock:
                self._flows[secid] = (items, stamp)
        return {"ok": True, "stale": False, "time": stamp, "list": list(items)}

    # ---- ETF 持仓（成分股行情 3 秒缓存，一次新浪请求拿全）-------------
    def _holdings_quote(self, secids: List[str]) -> Dict[str, Any]:
        wanted = list(dict.fromkeys(secid for secid in secids if secid))
        if not wanted:
            return {}
        key = tuple(sorted(wanted))
        cached = self._holdings_quotes.get(key)
        if cached is not None:
            return cached
        try:
            data = C.fetch_quotes_sina(wanted, timeout=8.0)
        except Exception:                # noqa: BLE001 - 行情失败不影响持仓展示
            data = {}
        self._holdings_quotes.put(key, data)
        return data

    def holdings_payload(self, code: str, full: bool) -> Dict[str, Any]:
        code = _text(code).strip()
        if not code:
            raise ApiError("缺少 code 参数")
        try:
            result = C.fetch_etf_holdings(code, full=bool(full))
        except Exception as exc:         # noqa: BLE001
            raise ApiError(f"持仓获取失败: {type(exc).__name__}: {exc}"[:200]) from exc
        if not getattr(result, "items", None):
            raise ApiError(_text(getattr(result, "error", "")) or "未获取到持仓数据")

        secid_of: Dict[str, str] = {}
        for holding in result.items:
            secid_of[holding.code] = C.etf_secid(holding.code)
        quotes = self._holdings_quote([secid for secid in secid_of.values() if secid])

        items: List[Dict[str, Any]] = []
        for holding in result.items:
            quote = quotes.get(secid_of.get(holding.code) or "")
            items.append({
                "code": holding.code,
                "name": holding.name or (_text(getattr(quote, "name", "")) if quote else ""),
                "weight": _r(holding.weight),
                "price": _r(getattr(quote, "price", 0.0)) if quote else 0.0,
                "change": _r(getattr(quote, "change", 0.0)) if quote else 0.0,
                "pct": _r(getattr(quote, "pct", 0.0)) if quote else 0.0,
                "ok": quote is not None,
            })
        return {
            "ok": True,
            "etf_code": _text(getattr(result, "etf_code", "")) or code,
            "etf_name": _text(getattr(result, "etf_name", "")),
            "period": _text(getattr(result, "period", "")),
            "kind": _text(getattr(result, "kind", "")) or ("full" if full else "top10"),
            "total_weight": _r(getattr(result, "total_weight", 0.0), 2),
            "source": _text(getattr(result, "source", "")) or "天天基金",
            "error": _text(getattr(result, "error", "")),
            "items": items,
        }

    def holdings_quotes_payload(self, raw_codes: str) -> Dict[str, Any]:
        """按 6 位股票代码批量取成分股行情 -> {code: {price, change, pct}}。

        前端持仓表是「先出持仓、再补行情」的两段式加载（`/api/holdings/quotes`），
        这里复用同一个 3 秒 `fetch_quotes_sina` 缓存，不会额外打接口。
        """
        codes = list(dict.fromkeys(
            part.strip() for part in _text(raw_codes).split(",") if part.strip()))
        if not codes:
            return {"ok": True, "quotes": {}}
        if len(codes) > 1000:
            raise ApiError("一次最多查询 1000 只股票")
        secid_of = {code: C.etf_secid(code) for code in codes}
        quotes = self._holdings_quote([secid for secid in secid_of.values() if secid])
        out: Dict[str, Any] = {}
        for code, secid in secid_of.items():
            quote = quotes.get(secid) if secid else None
            if quote is None:
                continue
            out[code] = {
                "code": code,
                "name": _text(getattr(quote, "name", "")),
                "price": _r(getattr(quote, "price", 0.0)),
                "change": _r(getattr(quote, "change", 0.0)),
                "pct": _r(getattr(quote, "pct", 0.0)),
            }
        return {"ok": True, "quotes": out}

    # ---- 搜索 ----------------------------------------------------------
    def search_payload(self, keyword: str, limit: int) -> Dict[str, Any]:
        keyword = _text(keyword).strip()
        if not keyword:
            return {"ok": True, "list": []}
        try:
            found = C.QuoteClient().search(keyword, timeout=6.0)
        except Exception as exc:         # noqa: BLE001
            raise ApiError(f"搜索失败: {type(exc).__name__}: {exc}"[:200]) from exc
        rows = [{"secid": _text(item.get("secid")), "code": _text(item.get("code")),
                 "name": _text(item.get("name"))}
                for item in (found or []) if item.get("secid")]
        return {"ok": True, "list": rows[:limit]}

    # ---- 自选增删改 ----------------------------------------------------
    def resolve_query(self, query: str) -> Optional[Dict[str, str]]:
        """用户输入 -> {'secid','code','name'}：先东财搜索，再本地符号解析，最后当 secid。"""
        query = _text(query).strip()
        if not query:
            return None
        try:
            found = C.QuoteClient().search(query, timeout=6.0)
        except Exception:                # noqa: BLE001 - 搜索失败还有下面的兜底
            found = []
        if found:
            best = found[0]
            secid = _text(best.get("secid"))
            if secid:
                code = _text(best.get("code")) or (secid.split(".", 1)[1] if "." in secid else secid)
                name = _text(best.get("name")) or self._quote_name(secid)
                return {"secid": secid, "code": code, "name": name}
        parsed = C.parse_symbol(query)
        if parsed and parsed.get("secid"):
            secid = _text(parsed["secid"])
            name = _text(parsed.get("name")) or self._quote_name(secid)
            code = _text(parsed.get("code")) or (secid.split(".", 1)[1] if "." in secid else secid)
            return {"secid": secid, "code": code, "name": name}
        head = query.split(".", 1)[0]
        if "." in query and head.isdigit():     # 已经是 secid 形式（如 1.600519）
            name = self._quote_name(query)
            if name:
                return {"secid": query, "code": query.split(".", 1)[1], "name": name}
        return None

    def watchlist_action(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError("请求体必须是 JSON 对象")
        action = _text(body.get("action")).strip().lower()
        if action == "add":
            return self._watchlist_add(body)
        if action == "remove":
            return self._watchlist_remove(body)
        if action == "move":
            return self._watchlist_move(body)
        raise ApiError(f"未知操作: {action or '(空)'}")

    def _watchlist_add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        query = _text(body.get("query")).strip()
        if not query:
            raise ApiError("缺少 query 参数")
        item = self.resolve_query(query)
        if not item:
            raise ApiError(f"未找到股票: {query}")
        label = item.get("name") or item.get("code") or item["secid"]
        with self._lock:
            if any(row["secid"] == item["secid"] for row in self.config.stocks()):
                raise ApiError(f"已在自选中: {label}")
            self.config.add_stock(item)
            self.config.save()           # 让桌面程序和网页共享同一份 stocks.json
            rows = self._stocks_for_api()
        return {"ok": True, "list": rows, "message": f"已添加 {label}"}

    def _watchlist_remove(self, body: Dict[str, Any]) -> Dict[str, Any]:
        secid = _text(body.get("secid")).strip()
        if not secid:
            raise ApiError("缺少 secid 参数")
        with self._lock:
            target = next((row for row in self.config.stocks() if row["secid"] == secid), None)
            if target is None:
                raise ApiError(f"自选中没有: {secid}")
            label = target.get("name") or target.get("code") or secid
            self.config.remove_stock(secid)
            self.config.save()
            rows = self._stocks_for_api()
        return {"ok": True, "list": rows, "message": f"已移除 {label}"}

    def _watchlist_move(self, body: Dict[str, Any]) -> Dict[str, Any]:
        secid = _text(body.get("secid")).strip()
        if not secid:
            raise ApiError("缺少 secid 参数")
        try:
            delta = int(body.get("delta"))
        except (TypeError, ValueError):
            raise ApiError("delta 需要是整数") from None
        if delta == 0:
            raise ApiError("delta 不能为 0")
        with self._lock:
            rows = self.config.stocks()
            index = next((idx for idx, row in enumerate(rows) if row["secid"] == secid), -1)
            if index < 0:
                raise ApiError(f"自选中没有: {secid}")
            label = rows[index].get("name") or rows[index].get("code") or secid
            self.config.move_stock(secid, delta)
            self.config.save()
            after = self._stocks_for_api()
        moved = next((idx for idx, row in enumerate(after) if row["secid"] == secid), index)
        if moved == index:
            message = f"{label} 已在{'最前' if delta < 0 else '最后'}"
        else:
            message = f"已{'上移' if delta < 0 else '下移'} {label}"
        return {"ok": True, "list": after, "message": message}

    # ---- 状态 ----------------------------------------------------------
    def status_payload(self) -> Dict[str, Any]:
        try:
            trading = bool(C.is_trading_time())
        except Exception:                # noqa: BLE001
            trading = False
        try:
            phase = _text(C.market_phase())
        except Exception:                # noqa: BLE001
            phase = ""
        with self._lock:
            refresh = _number(self.config.data.get("refresh_sec"), 3)
            idle = _number(self.config.data.get("idle_refresh_sec"), 60)
        return {
            "ok": True,
            "version": C.APP_VERSION,
            "phase": phase,
            "trading": trading,
            "refresh_sec": refresh,
            "idle_refresh_sec": idle,
            "server_time": time.strftime("%H:%M:%S"),
        }


# --------------------------------------------------------------------------
# API 路由（参数解析 -> WebApp）
# --------------------------------------------------------------------------
def _api_status(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.status_payload()


def _api_watchlist(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.watchlist_payload()


def _api_quotes(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.quotes_payload(_param(query, "secids", ""))


def _api_commodities(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.commodities_payload()


def _api_kline(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.kline_payload(
        secid=_param(query, "secid"),
        period=_param(query, "period", "daily") or "daily",
        adjust=_to_int(_param(query, "adjust", "1"), 1, 0, 2),
        limit=_to_int(_param(query, "limit", "180"), 180, 1, 1000),
    )


def _api_tick(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.tick_payload(_param(query, "secid"))


def _api_fundflow(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.fundflow_payload(
        secid=_param(query, "secid"),
        limit=_to_int(_param(query, "limit", "20"), 20, 1, 120),
    )


def _api_holdings(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    full = _param(query, "full", "0").strip().lower() in ("1", "true", "yes", "on")
    return app.holdings_payload(_param(query, "code"), full=full)


def _api_holdings_quotes(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.holdings_quotes_payload(_param(query, "codes"))


def _api_search(app: WebApp, query: Dict[str, List[str]]) -> Dict[str, Any]:
    return app.search_payload(
        keyword=_param(query, "q"),
        limit=_to_int(_param(query, "limit", "8"), 8, 1, 12),
    )


GET_ROUTES = {
    "/api/status": _api_status,
    "/api/watchlist": _api_watchlist,
    "/api/quotes": _api_quotes,
    "/api/commodities": _api_commodities,
    "/api/kline": _api_kline,
    "/api/tick": _api_tick,
    "/api/fundflow": _api_fundflow,
    "/api/holdings": _api_holdings,
    "/api/holdings/quotes": _api_holdings_quotes,
    "/api/search": _api_search,
}


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    """单一路由入口；请求行不落日志，异常不外泄。"""

    protocol_version = "HTTP/1.1"
    server_version = "StockWeb"
    sys_version = ""

    # 请求行静音（契约要求：只在启动时打印一行 URL）
    def log_message(self, fmt: str, *args: Any) -> None:     # noqa: A003
        return

    @property
    def app(self) -> WebApp:
        return self.server.app                    # type: ignore[attr-defined]

    # ---- HTTP 方法 -----------------------------------------------------
    def do_GET(self) -> None:                     # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:                    # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:                 # noqa: N802
        self._responded = True
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

    # ---- 分发 ----------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        self._responded = False
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path or "/"
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        except Exception:                         # noqa: BLE001 - 畸形请求一律 404
            return self._not_found()

        try:
            if method == "GET":
                if path in ("/", "/index.html"):
                    return self._serve_index()
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])
            if path.startswith("/api/"):
                return self._serve_api(method, path, query)
            return self._not_found()
        except Exception:                         # noqa: BLE001 - 最后一道兜底
            try:
                return self._not_found()
            except Exception:                     # noqa: BLE001
                return None

    def _serve_api(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        app = self.app
        try:
            if path == "/api/watchlist":
                if method == "POST":
                    return self._json(app.watchlist_action(self._read_json_body()))
                if method == "GET":
                    return self._json(app.watchlist_payload())
                return self._text(405, "405 Method Not Allowed")
            if method != "GET":
                return self._text(405, "405 Method Not Allowed")
            route = GET_ROUTES.get(path)
            if route is None:
                return self._not_found()
            return self._json(route(app, query))
        except ApiError as exc:                   # 业务失败：HTTP 200 + ok=false
            return self._json({"ok": False, "error": str(exc)})
        except Exception as exc:                  # noqa: BLE001 - 兜底，绝不让连接挂死
            return self._json({"ok": False,
                               "error": f"接口异常: {type(exc).__name__}: {exc}"[:200]})

    # ---- 静态资源 ------------------------------------------------------
    def _serve_index(self) -> None:
        path = os.path.join(WEB_DIR, "index.html")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._not_found()
        return self._send(200, body, "text/html; charset=utf-8")

    def _serve_static(self, raw_name: str) -> None:
        # 先 URL 解码再按白名单查表：白名单键里没有 "/" "\\" ".."，穿越名不可能命中
        name = urllib.parse.unquote(_text(raw_name).split("?", 1)[0])
        spec = STATIC_FILES.get(name)
        if spec is None:
            return self._not_found()
        # 双保险：规范化后必须仍在 web/ 目录内（白名单已挡住，这里防未来改动）
        web_root = os.path.realpath(WEB_DIR)
        path = os.path.realpath(os.path.join(web_root, spec[0]))
        if not path.startswith(web_root + os.sep):
            return self._not_found()
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._not_found()
        return self._send(200, body, spec[1])

    # ---- 响应 ----------------------------------------------------------
    def _json(self, payload: Dict[str, Any]) -> None:
        if getattr(self, "_responded", False):
            return
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            body = json.dumps({"ok": False, "error": "响应序列化失败"},
                              ensure_ascii=False).encode("utf-8")
        return self._send(200, body, "application/json; charset=utf-8")

    def _text(self, status: int, text: str) -> None:
        return self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _not_found(self) -> None:
        return self._text(404, "404 Not Found")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        if getattr(self, "_responded", False):
            return
        self._responded = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # 浏览器提前断开（切页/刷新）是常态，不能让它变成异常日志
            pass

    # ---- 请求体 --------------------------------------------------------
    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            raise ApiError("请求体为空（需要 JSON）")
        if length > 1024 * 1024:
            raise ApiError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("请求体不是合法 JSON") from None


# --------------------------------------------------------------------------
# 服务装配 / 命令行
# --------------------------------------------------------------------------
class PortUnavailable(RuntimeError):
    """指定端口及后续 10 个候选都被占用。"""


def build_server(port: int = DEFAULT_PORT, config_path: Optional[str] = None,
                 host: str = HOST, max_tries: int = PORT_TRIES) -> ThreadingHTTPServer:
    """建好（并绑定）服务，返回 ThreadingHTTPServer；port=0 由系统分配。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if port < 0:
        port = DEFAULT_PORT

    app = WebApp(config_path or default_config_path())
    httpd = None

    # Windows 下 SO_REUSEADDR 允许绑定「已被别的进程监听」的端口，会把请求
    # 悄悄分给旧进程；关掉它，端口占用才会如实抛 OSError，才能正确顺延。
    try:
        ThreadingHTTPServer.allow_reuse_address = False
    except Exception:                             # noqa: BLE001
        pass

    attempts = 1 if port == 0 else max(1, int(max_tries))
    last_exc: Optional[BaseException] = None
    for offset in range(attempts):
        candidate = port + offset
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
        except OSError as exc:
            last_exc = exc
            continue
        httpd.daemon_threads = True
        httpd.app = app                            # type: ignore[attr-defined]
        httpd.config_path = app.config_path        # type: ignore[attr-defined]
        return httpd

    raise PortUnavailable(
        f"端口 {port}~{port + attempts - 1} 都被占用，无法启动（可用 --port 换一个端口）"
    ) from last_exc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web_server.py",
        description="股票监控 · 本地网页版（标准库 HTTP 服务）",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}；0=由系统分配）")
    parser.add_argument("--config", default="", metavar="DIR",
                        help="数据目录（stocks.json / cache），默认与桌面程序相同")
    parser.add_argument("--open", action="store_true",
                        help="启动后自动用默认浏览器打开")
    args = parser.parse_args(argv)

    if args.config:
        config_dir = os.path.abspath(os.path.expanduser(args.config))
        try:
            os.makedirs(config_dir, exist_ok=True)
        except OSError as exc:
            print(f"数据目录创建失败: {config_dir}（{exc}）")
            return 1
        config_path = os.path.join(config_dir, "stocks.json")
    else:
        config_path = default_config_path()

    try:
        httpd = build_server(port=args.port, config_path=config_path)
    except PortUnavailable as exc:
        print(f"启动失败: {exc}")
        return 1

    actual_port = int(httpd.server_address[1])
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"网页版已启动: {url}", flush=True)     # 契约：启动时打印一行 URL
    if args.port and actual_port != args.port:
        print(f"（端口 {args.port} 被占用，已改用 {actual_port}）", flush=True)

    if args.open:
        def _open_browser() -> None:
            time.sleep(0.4)                       # 等 serve_forever 真正开始
            try:
                webbrowser.open(url)
            except Exception:                     # noqa: BLE001 - 没默认浏览器也不影响服务
                pass

        threading.Thread(target=_open_browser, name="open-browser", daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:                     # Ctrl+C 正常退出
        print("\n已停止")
    finally:
        try:
            httpd.server_close()
        except Exception:                         # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
