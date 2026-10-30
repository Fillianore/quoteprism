# -*- coding: utf-8 -*-
"""网页版后端测试（真实 HTTP 服务 + 系统随机端口，纯标准库）。

用法：
    python tests/test_web_server.py

做法：
  · 在**进程内**用 `web_server.build_server(port=0, ...)` 起一个真实的
    ThreadingHTTPServer（端口由系统分配），再用 urllib.request 打自己的接口；
  · 配置目录指向临时目录（`--config`），自选增删改只写临时 stocks.json，
    绝不污染用户的真实配置；
  · 跑完 shutdown + server_close，进程正常退出。

覆盖：
  1.  /api/status           200 且 ok/version/phase 齐全
  2.  /api/watchlist        列表字段齐全（secid/code/name）
  3.  /api/quotes           省略 secids 跟随自选；显式 secids 只返回请求项
  4.  /api/kline            bars 非空，ma5/ma20 与 bars 等长
  5.  /api/tick             ok=true（bars 允许为空）
  6.  /api/commodities      列表非空
  7.  /api/holdings         非空（网络失败 SKIP）
  8.  /api/search           q=茅台 有结果（网络失败 SKIP）
  9.  未知路径              404 文本
  10. 静态资源              /static/app.js、/static/style.css（存在=200，不存在=404）+ 目录穿越 404
  11. 并发                  同时 5 个 /api/status 全部成功（验证 ThreadingHTTPServer）
  额外：CORS/Content-Type 头、业务失败 ok=false、POST /api/watchlist、/api/fundflow、
        /api/holdings/quotes（前端持仓表补行情用）

退出码：0 = 没有 FAIL（可以有 SKIP）；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _make_temp_config_dir() -> str:
    """建一个可写的临时目录（配置 + stock_core 磁盘缓存都放这里）。

    注意用 `os.makedirs`（默认权限）而不是 `tempfile.mkdtemp`（0o700）：
    某些受限环境会把 0o700 目录映射成「创建者自己都写不进去」的 ACL。
    系统临时目录不可写时（只读安装 / 特殊沙箱），回退到测试目录旁边。
    """
    bases = [tempfile.gettempdir(), HERE]
    for base in bases:
        path = os.path.join(base, f"_web_tmp_{os.getpid()}_{int(time.time())}")
        config_dir = os.path.join(path, "data")
        try:
            os.makedirs(config_dir, exist_ok=True)
            probe = os.path.join(config_dir, ".write_probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.remove(probe)
            return path
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
    raise RuntimeError("找不到可写的临时目录（系统临时目录和测试目录都不可写）")


TMPDIR = _make_temp_config_dir()
CONFIG_DIR = os.path.join(TMPDIR, "data")
CONFIG_PATH = os.path.join(CONFIG_DIR, "stocks.json")
os.makedirs(CONFIG_DIR, exist_ok=True)

# 必须在 import web_server 之前指定 --config：stock_core 的磁盘缓存/日志
# 也会跟着定位到临时目录，不会写进项目目录。
sys.argv = [sys.argv[0], "--config", CONFIG_DIR]
sys.path.insert(0, ROOT)
try:  # Windows 控制台默认 GBK，统一成 UTF-8 免得打印中文炸掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import web_server as W  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0
BASE = ""
HTTPD = None
SERVER_THREAD = None


# --------------------------------------------------------------------------
# 断言 / HTTP 工具
# --------------------------------------------------------------------------
def check(name: str, cond: bool, detail: str = "") -> bool:
    """一条断言：True -> PASS，False -> FAIL。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))
    return bool(cond)


def skip(name: str, reason: str) -> None:
    """网络/接口不可用：明确打印 SKIP，不计失败。"""
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name}  <- {reason}")


def request(method: str, path: str, payload=None, timeout: float = 60.0):
    """发一个 HTTP 请求，返回 (status, headers_dict, body_bytes)；HTTP 错误也返回。"""
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get_raw(path: str, timeout: float = 60.0):
    return request("GET", path, timeout=timeout)


def get_json(path: str, timeout: float = 60.0):
    status, headers, body = request("GET", path, timeout=timeout)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    return status, headers, data, body


def post_json(path: str, payload, timeout: float = 60.0):
    status, headers, body = request("POST", path, payload=payload, timeout=timeout)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    return status, headers, data, body


def is_zh(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


# --------------------------------------------------------------------------
# 1. 状态
# --------------------------------------------------------------------------
def test_status() -> None:
    print("\n[1] /api/status")
    status, headers, data, _ = get_json("/api/status")
    data = data or {}
    check("HTTP 200", status == 200, f"status={status}")
    check("Content-Type 是 application/json; charset=utf-8",
          headers.get("Content-Type") == "application/json; charset=utf-8",
          f"ct={headers.get('Content-Type')!r}")
    check("CORS 头 Access-Control-Allow-Origin: *",
          headers.get("Access-Control-Allow-Origin") == "*",
          f"acao={headers.get('Access-Control-Allow-Origin')!r}")
    check("ok=true", data.get("ok") is True, f"data={data}")
    check("version 非空（桌面程序同版本号）", bool(data.get("version")), f"version={data.get('version')!r}")
    check("phase 是中文阶段字符串", isinstance(data.get("phase"), str) and is_zh(data.get("phase")),
          f"phase={data.get('phase')!r}")
    check("trading 是 bool", isinstance(data.get("trading"), bool), f"trading={data.get('trading')!r}")
    check("refresh_sec / idle_refresh_sec 是数字",
          isinstance(data.get("refresh_sec"), (int, float))
          and isinstance(data.get("idle_refresh_sec"), (int, float)),
          f"refresh={data.get('refresh_sec')!r} idle={data.get('idle_refresh_sec')!r}")
    check("server_time 形如 HH:MM:SS",
          isinstance(data.get("server_time"), str)
          and re.match(r"^\d{2}:\d{2}:\d{2}$", data["server_time"]) is not None,
          f"server_time={data.get('server_time')!r}")


# --------------------------------------------------------------------------
# 2. 自选
# --------------------------------------------------------------------------
def test_watchlist():
    print("\n[2] /api/watchlist")
    status, headers, data, _ = get_json("/api/watchlist")
    data = data or {}
    rows = data.get("list")
    check("HTTP 200", status == 200, f"status={status}")
    check("ok=true 且 list 是数组", data.get("ok") is True and isinstance(rows, list),
          f"data={str(data)[:120]}")
    if not isinstance(rows, list):
        rows = []
    check("自选非空（临时配置里是默认自选）", len(rows) > 0, f"len={len(rows)}")
    bad = [row for row in rows
           if not isinstance(row, dict)
           or not isinstance(row.get("secid"), str) or not row["secid"]
           or not isinstance(row.get("code"), str) or not row["code"]
           or not isinstance(row.get("name"), str) or not row["name"]]
    check("每项都有非空 secid / code / name", not bad, f"bad={bad[:2]}")
    check("secid 形如 市场.代码",
          all(re.match(r"^\d+\.[0-9A-Za-z]+$", str(row.get("secid"))) for row in rows),
          f"secids={[row.get('secid') for row in rows][:6]}")
    return rows


# --------------------------------------------------------------------------
# 3. 行情（跟随自选 / 显式 secids）
# --------------------------------------------------------------------------
QUOTE_FIELDS = ("secid", "code", "name", "price", "prev_close", "change", "pct",
                "open", "high", "low", "volume_ratio", "turnover",
                "main_net", "main_net_pct", "quote_time", "live", "ok")


def test_quotes(watch_rows) -> None:
    print("\n[3] /api/quotes")
    status, headers, data, _ = get_json("/api/quotes")
    data = data or {}
    rows = data.get("list") or []
    check("HTTP 200 且 Content-Type 是 JSON",
          status == 200 and headers.get("Content-Type") == "application/json; charset=utf-8",
          f"status={status} ct={headers.get('Content-Type')!r}")
    check("ok=true", data.get("ok") is True, f"data={str(data)[:120]}")
    check("省略 secids 时跟随自选（条数 > 0）", len(rows) > 0, f"len={len(rows)}")
    check("返回条数 = 自选条数", len(rows) == len(watch_rows),
          f"{len(rows)} vs {len(watch_rows)}")
    check("顺序与自选顺序一致（即自选顺序）",
          [row.get("secid") for row in rows] == [row.get("secid") for row in watch_rows],
          f"got={[row.get('secid') for row in rows][:6]}")
    missing = [field for field in QUOTE_FIELDS if field not in (rows[0] if rows else {})]
    check("报价字段齐全", not missing, f"missing={missing}")
    check("price/prev_close/pct 都是数字",
          all(isinstance(row.get(field), (int, float))
              for row in rows for field in ("price", "prev_close", "pct", "change")),
          "存在非数字字段")
    real = [row for row in rows if row.get("ok") is True]
    if real:
        check("至少一只有真实行情（ok=true）", True)
        check("真实行情有价格或昨收",
              any((row.get("price") or 0) > 0 or (row.get("prev_close") or 0) > 0 for row in real),
              f"prices={[row.get('price') for row in real][:5]}")
    else:
        skip("至少一只有真实行情", "行情源不可用，本次全是占位项（ok=false）")

    want = [row["secid"] for row in watch_rows[:2]] or ["1.600519"]
    status, headers, data, _ = get_json("/api/quotes?secids=" + urllib.parse.quote(",".join(want)))
    data = data or {}
    got = [row.get("secid") for row in (data.get("list") or [])]
    check("显式 secids：命中 200 + ok=true", status == 200 and data.get("ok") is True,
          f"status={status} data={str(data)[:120]}")
    check("显式 secids 只返回请求的那几只（按请求顺序）", got == want, f"got={got} want={want}")
    check("显式 secids 条数正确", len(got) == len(want), f"len={len(got)}")


# --------------------------------------------------------------------------
# 4. K 线
# --------------------------------------------------------------------------
def test_kline() -> None:
    print("\n[4] /api/kline")
    status, headers, data, _ = get_json(
        "/api/kline?secid=1.600519&period=daily&adjust=1&limit=60", timeout=90)
    data = data or {}
    if data.get("ok") is not True:
        skip("K 线接口", f"接口不可用: {data.get('error')}")
        return
    check("HTTP 200 + ok=true", status == 200, f"status={status}")
    check("secid / name / period / period_label / source 字段齐全",
          data.get("secid") == "1.600519" and bool(data.get("name"))
          and data.get("period") == "daily" and bool(data.get("period_label"))
          and bool(data.get("source")),
          f"keys={ {k: data.get(k) for k in ('secid', 'name', 'period', 'period_label', 'source')} }")
    bars = data.get("bars") or []
    ma5 = data.get("ma5") or []
    ma20 = data.get("ma20") or []
    check("bars 长度 > 0", len(bars) > 0, f"len={len(bars)}")
    check("bars 不超过 limit(60)", len(bars) <= 60, f"len={len(bars)}")
    check("ma5 长度 == bars 长度", len(ma5) == len(bars), f"{len(ma5)} vs {len(bars)}")
    check("ma20 长度 == bars 长度", len(ma20) == len(bars), f"{len(ma20)} vs {len(bars)}")
    bar_fields = ("date", "open", "high", "low", "close", "volume", "amount", "pct")
    check("bar 字段齐全", all(field in (bars[0] if bars else {}) for field in bar_fields),
          f"bar0={bars[0] if bars else None}")
    check("bars 按日期升序",
          [bar.get("date") for bar in bars] == sorted(bar.get("date") for bar in bars),
          "日期不是升序")
    check("ma5 前 4 个是 null（数据不足）",
          all(value is None for value in ma5[:4]), f"ma5[:5]={ma5[:5]}")

    # 业务失败：非法 period 仍然 HTTP 200 + ok=false
    status, headers, data, _ = get_json("/api/kline?secid=1.600519&period=2min")
    data = data or {}
    check("非法 period -> HTTP 200 + ok=false + 中文 error",
          status == 200 and data.get("ok") is False and is_zh(data.get("error")),
          f"status={status} data={str(data)[:120]}")


# --------------------------------------------------------------------------
# 5. 分时
# --------------------------------------------------------------------------
def test_tick() -> None:
    print("\n[5] /api/tick")
    status, headers, data, _ = get_json("/api/tick?secid=1.512050", timeout=90)
    data = data or {}
    if data.get("ok") is not True:
        skip("分时接口", f"接口不可用: {data.get('error')}")
        return
    check("HTTP 200 + ok=true", status == 200, f"status={status}")
    bars = data.get("bars")
    check("bars 是数组（允许为空）", isinstance(bars, list), f"type={type(bars).__name__}")
    check("flow 是数组（取不到时给 []）", isinstance(data.get("flow"), list),
          f"type={type(data.get('flow')).__name__}")
    check("date / same_day / prev_close / coverage 字段齐全",
          isinstance(data.get("date"), str) and isinstance(data.get("same_day"), bool)
          and isinstance(data.get("prev_close"), (int, float))
          and isinstance(data.get("coverage"), int),
          f"date={data.get('date')!r} same_day={data.get('same_day')!r} "
          f"prev_close={data.get('prev_close')!r} coverage={data.get('coverage')!r}")
    if not bars:
        skip("分时 bars 明细", "bars 为空（停牌 / 未开盘，契约允许）")
        return
    check("bar 字段齐全（t/minute/close/avg/volume）",
          all(field in bars[0] for field in ("t", "minute", "close", "avg", "volume")),
          f"bar0={bars[0]}")
    check("minute 落在 0..240", all(0 <= bar.get("minute", -1) <= 240 for bar in bars),
          f"minutes={[bar.get('minute') for bar in bars][:6]}")
    check("t 形如 HH:MM", all(re.match(r"^\d{2}:\d{2}$", str(bar.get("t"))) for bar in bars[:5]),
          f"ts={[bar.get('t') for bar in bars][:5]}")
    check("coverage <= 240", data.get("coverage") <= 240, f"coverage={data.get('coverage')}")


# --------------------------------------------------------------------------
# 6. 通货
# --------------------------------------------------------------------------
def test_commodities() -> None:
    print("\n[6] /api/commodities")
    status, headers, data, _ = get_json("/api/commodities", timeout=90)
    data = data or {}
    check("HTTP 200 + ok=true", status == 200 and data.get("ok") is True,
          f"status={status} data={str(data)[:120]}")
    rows = data.get("list") or []
    if not rows:
        skip("通货列表非空", "行情源不可用")
        return
    check("列表非空", True, f"len={len(rows)}")
    fields = ("code", "name", "price", "prev_close", "change", "pct", "unit",
              "stale", "quote_time", "source")
    check("每项字段齐全", all(field in rows[0] for field in fields), f"missing={[f for f in fields if f not in rows[0]]}")
    check("至少一项有价格", any((row.get("price") or 0) > 0 for row in rows),
          f"prices={[row.get('price') for row in rows][:5]}")
    check("stale 是 bool", all(isinstance(row.get("stale"), bool) for row in rows),
          "存在非 bool 的 stale")


# --------------------------------------------------------------------------
# 7. ETF 持仓
# --------------------------------------------------------------------------
def test_holdings():
    print("\n[7] /api/holdings")
    status, headers, data, _ = get_json("/api/holdings?code=512050", timeout=120)
    data = data or {}
    if data.get("ok") is not True:
        skip("ETF 持仓", f"接口不可用: {data.get('error')}")
        return None
    check("HTTP 200 + ok=true", status == 200, f"status={status}")
    items = data.get("items") or []
    if not items:
        check("items 非空", False, "items=[]")
        return None
    check("items 非空", True, f"len={len(items)}")
    check("etf_code / etf_name / period / kind / source 字段齐全",
          data.get("etf_code") == "512050" and bool(data.get("etf_name"))
          and bool(data.get("period")) and data.get("kind") in ("top10", "full")
          and bool(data.get("source")),
          f"keys={ {k: data.get(k) for k in ('etf_code', 'etf_name', 'period', 'kind', 'source')} }")
    check("total_weight 是数字且 <= 100",
          isinstance(data.get("total_weight"), (int, float)) and 0 < data["total_weight"] <= 100,
          f"total_weight={data.get('total_weight')!r}")
    check("error 字段存在（成功时为 \"\"）", data.get("error") == "", f"error={data.get('error')!r}")
    check("item 字段齐全（code/name/weight）",
          all(all(field in item for field in ("code", "name", "weight")) for item in items),
          f"item0={items[0]}")
    weights = [round(float(item.get("weight") or 0), 4) for item in items]
    check("items 按权重降序", weights == sorted(weights, reverse=True), f"weights={weights[:6]}")
    check("top10 数量合理（<= 20）", len(items) <= 20, f"len={len(items)}")

    quoted = [item for item in items if (item.get("price") or 0) > 0]
    if quoted:
        check("items 里附加了成分股行情（price/change/pct）",
              all(all(field in item for field in ("price", "change", "pct")) for item in items),
              f"quoted={len(quoted)}/{len(items)}")
    else:
        skip("items 附加成分股行情", "新浪行情不可用")

    # 前端持仓表会额外调 /api/holdings/quotes 补行情（契约外的扩展接口）
    codes = ",".join(str(item.get("code")) for item in items)
    status, headers, data2, _ = get_json("/api/holdings/quotes?codes=" + urllib.parse.quote(codes),
                                         timeout=90)
    data2 = data2 or {}
    check("/api/holdings/quotes 返回 200 + ok=true + quotes 字典",
          status == 200 and data2.get("ok") is True and isinstance(data2.get("quotes"), dict),
          f"status={status} data={str(data2)[:120]}")
    quotes = data2.get("quotes") or {}
    if quotes:
        one = next(iter(quotes.values()))
        check("quotes 项含 price/change/pct",
              all(field in one for field in ("price", "change", "pct")), f"one={one}")
    else:
        skip("/api/holdings/quotes 有行情", "新浪行情不可用")
    return items


# --------------------------------------------------------------------------
# 8. 搜索
# --------------------------------------------------------------------------
def test_search() -> None:
    print("\n[8] /api/search")
    status, headers, data, _ = get_json("/api/search?q=" + urllib.parse.quote("茅台") + "&limit=8",
                                        timeout=60)
    data = data or {}
    check("HTTP 200 + ok=true", status == 200 and data.get("ok") is True,
          f"status={status} data={str(data)[:120]}")
    rows = data.get("list") or []
    if not rows:
        skip("搜索 q=茅台 有结果", "搜索接口不可用（网络）")
        return
    check("有搜索结果", True, f"len={len(rows)}")
    check("结果含 secid / code / name",
          all(all(field in row for field in ("secid", "code", "name")) for row in rows),
          f"rows={rows[:2]}")
    check("结果里能找到贵州茅台", any("茅台" in str(row.get("name")) for row in rows),
          f"names={[row.get('name') for row in rows]}")
    check("limit 生效（<= 8）", len(rows) <= 8, f"len={len(rows)}")


# --------------------------------------------------------------------------
# 9. 未知路径 404
# --------------------------------------------------------------------------
def test_not_found() -> None:
    print("\n[9] 未知路径 404")
    for path in ("/api/does-not-exist", "/no/such/path", "/index.htm"):
        status, headers, body = get_raw(path)
        check(f"{path} 返回 404", status == 404, f"status={status}")
        check(f"{path} 是文本响应",
              "text/" in headers.get("Content-Type", ""),
              f"ct={headers.get('Content-Type')!r}")


# --------------------------------------------------------------------------
# 10. 静态资源 + 目录穿越
# --------------------------------------------------------------------------
def test_static() -> None:
    print("\n[10] 静态资源 / 防目录穿越")
    web_dir = os.path.join(ROOT, "web")
    for name, type_hint in (("app.js", "javascript"), ("style.css", "css")):
        status, headers, body = get_raw(f"/static/{name}")
        disk = os.path.join(web_dir, name)
        if os.path.isfile(disk):
            with open(disk, "rb") as fh:
                expect = fh.read()
            check(f"/static/{name} 文件存在时返回 200", status == 200, f"status={status}")
            check(f"/static/{name} 内容与磁盘一致", body == expect,
                  f"len={len(body)} vs {len(expect)}")
            check(f"/static/{name} Content-Type 含 {type_hint}",
                  type_hint in headers.get("Content-Type", ""),
                  f"ct={headers.get('Content-Type')!r}")
        else:
            check(f"/static/{name} 文件不存在时返回 404", status == 404, f"status={status}")

    index = os.path.join(web_dir, "index.html")
    status, headers, body = get_raw("/")
    if os.path.isfile(index):
        with open(index, "rb") as fh:
            expect = fh.read()
        check("GET / 返回 index.html", status == 200 and body == expect,
              f"status={status} len={len(body)}")
        check("GET / 的 Content-Type 是 text/html",
              "text/html" in headers.get("Content-Type", ""),
              f"ct={headers.get('Content-Type')!r}")
    else:
        check("GET / 在 index.html 缺失时返回 404", status == 404, f"status={status}")

    # 目录穿越必须 404（明文 / 编码 / 反斜杠 混写都试）
    for path in ("/static/../stock_core.py",
                 "/static/%2e%2e%2fstock_core.py",
                 "/static/..%2fstock_core.py",
                 "/static/%2e%2e/stock_core.py",
                 "/static/../../stocks.json"):
        status, headers, body = get_raw(path)
        leaked = b"APP_VERSION" in body or b"stocks" in body
        check(f"目录穿越 {path} 返回 404", status == 404, f"status={status}")
        check(f"目录穿越 {path} 没有泄露源码/配置", not leaked, f"body[:80]={body[:80]!r}")


# --------------------------------------------------------------------------
# 11. 并发（ThreadingHTTPServer 生效）
# --------------------------------------------------------------------------
def test_concurrency() -> None:
    print("\n[11] 并发 5 个 /api/status")
    check("服务是 ThreadingHTTPServer", isinstance(HTTPD, ThreadingHTTPServer),
          f"type={type(HTTPD).__name__}")
    results = [None] * 5
    barrier = threading.Barrier(5)
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            status, headers, data = get_json("/api/status", timeout=30)[0:3]
            with lock:
                results[idx] = (status, (data or {}).get("ok"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results[idx] = ("EXC", f"{type(exc).__name__}: {exc}")

    started = time.time()
    threads = [threading.Thread(target=worker, args=(idx,), daemon=True) for idx in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    alive = [thread for thread in threads if thread.is_alive()]
    check("5 个并发请求都及时返回", not alive, f"alive={len(alive)}")
    bad = [result for result in results if result != (200, True)]
    check("并发 5 个 /api/status 全部成功（200 + ok=true）", not bad, f"bad={bad}")
    check("并发总耗时 < 20s", (time.time() - started) < 20, f"{time.time() - started:.2f}s")


# --------------------------------------------------------------------------
# 额外：业务失败契约 / 资金流 / 自选增删改（临时配置）
# --------------------------------------------------------------------------
def test_business_errors() -> None:
    print("\n[12] 业务失败契约（HTTP 200 + ok=false + 中文 error）")
    for path in ("/api/kline", "/api/tick", "/api/fundflow", "/api/holdings"):
        status, headers, data, _ = get_json(path)
        data = data or {}
        check(f"{path} 缺参数 -> 200 + ok=false + 中文原因",
              status == 200 and data.get("ok") is False and is_zh(data.get("error")),
              f"status={status} data={str(data)[:120]}")
    status, headers, data, _ = post_json("/api/watchlist", {"action": "bogus"})
    data = data or {}
    check("POST 未知 action -> 200 + ok=false + 中文原因",
          status == 200 and data.get("ok") is False and is_zh(data.get("error")),
          f"status={status} data={str(data)[:120]}")
    # 空 body / 非法 JSON 也不能 500
    status, headers, body = request("POST", "/api/watchlist", payload=None)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    check("POST 空 body -> 200 + ok=false", status == 200 and (data or {}).get("ok") is False,
          f"status={status} body={body[:80]!r}")
    req = urllib.request.Request(BASE + "/api/watchlist", data=b"{not json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    check("POST 非法 JSON -> 200 + ok=false", status == 200 and (data or {}).get("ok") is False,
          f"status={status} body={raw[:80]!r}")


def test_fundflow() -> None:
    print("\n[13] /api/fundflow")
    status, headers, data, _ = get_json("/api/fundflow?secid=1.600519&limit=20", timeout=90)
    data = data or {}
    if data.get("ok") is not True:
        skip("资金流接口", f"接口不可用: {data.get('error')}")
        return
    rows = data.get("list") or []
    check("HTTP 200 + ok=true", status == 200, f"status={status}")
    check("stale 是 bool、time 形如 HH:MM:SS",
          isinstance(data.get("stale"), bool)
          and re.match(r"^\d{2}:\d{2}:\d{2}$", str(data.get("time"))) is not None,
          f"stale={data.get('stale')!r} time={data.get('time')!r}")
    check("list 非空且 <= limit", 0 < len(rows) <= 20, f"len={len(rows)}")
    fields = ("date", "close", "pct", "main_net", "super_net", "big_net", "mid_net", "small_net")
    check("资金流字段齐全", all(field in (rows[0] if rows else {}) for field in fields),
          f"row0={rows[0] if rows else None}")


def test_watchlist_post() -> None:
    print("\n[14] POST /api/watchlist（临时配置，验证 save 落盘）")
    status, headers, data, _ = get_json("/api/watchlist")
    rows = (data or {}).get("list") or []
    if len(rows) < 2:
        skip("POST /api/watchlist", "自选不足 2 只，跳过增删改")
        return

    # move：把最后一只上移一位
    target = rows[-1]["secid"]
    before = [row["secid"] for row in rows]
    status, headers, data, _ = post_json("/api/watchlist",
                                         {"action": "move", "secid": target, "delta": -1})
    data = data or {}
    after = [row["secid"] for row in (data.get("list") or [])]
    check("POST move -> 200 + ok=true + 中文 message",
          status == 200 and data.get("ok") is True and is_zh(data.get("message")),
          f"status={status} data={str(data)[:140]}")
    check("move 后位置前移一位",
          target in after and after.index(target) == before.index(target) - 1,
          f"before={before} after={after}")

    # remove
    status, headers, data, _ = post_json("/api/watchlist",
                                         {"action": "remove", "secid": target})
    data = data or {}
    after = [row["secid"] for row in (data.get("list") or [])]
    check("POST remove -> 200 + ok=true 且列表里消失",
          status == 200 and data.get("ok") is True and target not in after,
          f"status={status} data={str(data)[:140]}")

    # 再删同一条 -> 业务失败
    status, headers, data, _ = post_json("/api/watchlist",
                                         {"action": "remove", "secid": target})
    data = data or {}
    check("POST remove 不存在的项 -> 200 + ok=false + 中文原因",
          status == 200 and data.get("ok") is False and is_zh(data.get("error")),
          f"status={status} data={str(data)[:140]}")

    # add：query 交给搜索解析（网络失败 SKIP）
    status, headers, data, _ = post_json("/api/watchlist",
                                         {"action": "add", "query": "512050"})
    data = data or {}
    if data.get("ok") is not True:
        skip("POST add", f"添加失败（可能是搜索不可用）: {data.get('error')}")
        return
    added = [row for row in (data.get("list") or []) if row.get("secid") == "1.512050"]
    check("POST add 512050 -> 200 + ok=true + 列表里出现", bool(added),
          f"status={status} data={str(data)[:140]}")
    check("stocks.json 已写入临时配置目录", os.path.isfile(CONFIG_PATH), f"path={CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    check("磁盘配置包含新增的 secid（save 生效）",
          any(str(item.get("secid")) == "1.512050" for item in (saved.get("stocks") or [])),
          f"saved={[item.get('secid') for item in (saved.get('stocks') or [])]}")
    check("stocks.json 是合法 JSON 且未被写到项目目录",
          isinstance(saved, dict) and os.path.abspath(CONFIG_PATH).startswith(TMPDIR),
          f"config_path={CONFIG_PATH}")

    # 重复添加 -> 业务失败
    status, headers, data, _ = post_json("/api/watchlist",
                                         {"action": "add", "query": "512050"})
    data = data or {}
    check("POST add 重复 -> 200 + ok=false + 中文原因",
          status == 200 and data.get("ok") is False and is_zh(data.get("error")),
          f"status={status} data={str(data)[:140]}")


# --------------------------------------------------------------------------
# 启动 / 收尾
# --------------------------------------------------------------------------
def wait_ready(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, headers, data, _ = get_json("/api/status", timeout=2)
            if status == 200:
                return True
        except Exception:  # noqa: BLE001 - 还没起来
            pass
        time.sleep(0.05)
    return False


def main() -> int:
    global BASE, HTTPD, SERVER_THREAD, FAIL
    print("=" * 72)
    print("股票监控 · 网页版后端测试")
    print(f"模块: {W.__file__}")
    print(f"临时配置目录: {CONFIG_DIR}")
    print("=" * 72)

    try:
        HTTPD = W.build_server(port=0, config_path=CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 服务启动失败: {type(exc).__name__}: {exc}")
        return 1
    port = int(HTTPD.server_address[1])
    BASE = f"http://127.0.0.1:{port}"
    SERVER_THREAD = threading.Thread(target=HTTPD.serve_forever, name="web-test", daemon=True)
    SERVER_THREAD.start()
    print(f"实际监听: {BASE}/  （port=0，由系统分配）")
    print("=" * 72)

    if not wait_ready():
        print("[FAIL] 服务在 10s 内没有响应 /api/status")
        HTTPD.shutdown()
        HTTPD.server_close()
        return 1

    try:
        test_status()
        watch_rows = test_watchlist() or []
        test_quotes(watch_rows)
        test_kline()
        test_tick()
        test_commodities()
        test_holdings()
        test_search()
        test_not_found()
        test_static()
        test_concurrency()
        test_business_errors()
        test_fundflow()
        test_watchlist_post()
    except Exception:  # noqa: BLE001 - 测试自身异常也算 FAIL，别静默
        print("[FAIL] 测试过程抛出未捕获异常：")
        traceback.print_exc()
        FAIL += 1
    finally:
        print("\n关闭服务…")
        try:
            HTTPD.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            HTTPD.server_close()
        except Exception:  # noqa: BLE001
            pass
        if SERVER_THREAD is not None:
            SERVER_THREAD.join(timeout=5)

    print("\n" + "=" * 72)
    print(f"统计: PASS {PASS}  FAIL {FAIL}  SKIP {SKIP}")
    if SKIP:
        print("（SKIP 表示联网/接口不可用，按题目要求不算失败）")
    print("=" * 72)

    # 清理临时目录（配置 + 缓存），失败也无所谓
    shutil.rmtree(TMPDIR, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
