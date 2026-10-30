# QuotePrism

> 轻量级 A 股 / ETF 看盘网页：本地起一个 Python 标准库 HTTP 服务，浏览器打开就能看。
> 零第三方依赖，零外部 CDN，断网也能跑（只是没数据）。

一个**本地运行的行情看板**。后端只用 Python 标准库（`ThreadingHTTPServer`），
前端是原生 JS + Canvas 手绘图表（不引任何图表库）。数据来自东方财富 / 新浪 / 腾讯，
多源自动降级。

```
python web_server.py --open      # 打开 http://127.0.0.1:8770/
```

![status](https://img.shields.io/badge/python-3.10%2B-blue)
![status](https://img.shields.io/badge/dependencies-none-brightgreen)
![status](https://img.shields.io/badge/license-MIT-green)

---

## 功能

| 模块 | 说明 |
| --- | --- |
| 自选股 | 现价 / 涨跌 / 涨跌幅 / 主力净额，红涨绿跌，点表头排序，增删与上下移 |
| 图表 | **分时 / 5分 / 15分 / 30分 / 60分 / 日K / 周K / 月K**，不复权 / 前复权 / 后复权 |
| 分时 | 固定全天时间轴（09:30–11:30 / 13:00–15:00 午休折叠）、自适应刻度（放大可到半点） |
| 十字光标 | 横竖虚线 + 右侧价格轴 + 底部时间轴 + **跟随鼠标的详情框**（分时 / K线 / 资金流柱都有） |
| 显示 | 价格规则 `<10 三位 / ≥10 两位`（带千分位）；现价+涨跌幅 **18px 加粗**；曲线 2.6px |
| 缩放 | 分时滚轮缩放（最细 3 分钟视野）、拖动平移、双击复位；K线滚轮改可见根数 |
| 资金流 | 主力净流入柱状图（带悬停详情）+ 当日资金流分时曲线 |
| 全球通货 | 纽约金 / 银 / 原油 / 布伦特 / 沪金 / 沪银 / 燃油 / 美元兑人民币 / 美元指数 |
| **ETF 持仓** | 权重 + 成分股实时价/涨跌/贡献，可切换「前十大」↔「全部持仓」 |
| 布局 | 自选栏宽度可拖拽（300–640px，记忆），窄宽度时按优先级自动隐藏次要列 |

### 曲线平滑：好看但**不失真**

分时原始数据每分钟一个点，直接连线是硬折角。这里用 **Fritsch–Carlson 单调三次插值（PCHIP）**
做平滑，而不是普通样条，因为普通样条会**过冲**——凭空造出数据里没有的高点/低点。

PCHIP 的保证：

- 曲线**精确经过每一个真实数据点**（误差 0）
- 相邻两点之间**不越出这两点的取值范围** → 不会造假的高低点
- 原始点处一阶导连续（C1），所以看起来是顺的
- 原始局部极值点的高度原样保留

实测 240 点数据最大越界 `1.8e-15`（等于 0）。想关掉：`stocks.json` 里 `tick_smooth: false`。

---

## 快速开始

需要 **Python 3.10+**，不需要 `pip install` 任何东西。

```bash
python web_server.py                 # 默认 127.0.0.1:8770
python web_server.py --open          # 启动后自动打开浏览器
python web_server.py --port 8780     # 换端口（被占用会自动顺延）
python web_server.py --config ./data # 换数据目录（默认项目根）
```

Windows 上也可以双击 `run.bat`。

首次运行会自动在数据目录生成 `stocks.json`（自选股 + 配置），并预置几只示例。

---

## HTTP API

所有 `/api/*` 返回 `Content-Type: application/json; charset=utf-8`，带 `Access-Control-Allow-Origin: *`。
**业务失败也是 HTTP 200**，body 为 `{"ok": false, "error": "原因"}`，看 `ok` 字段判断。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 运行状态、交易时段、刷新间隔 |
| GET | `/api/watchlist` | 自选股列表（顺序即自选顺序） |
| GET | `/api/quotes?secids=...` | 实时行情；省略则用自选 |
| GET | `/api/commodities` | 贵金属 / 原油 / 外汇 |
| GET | `/api/kline?secid=&period=&adjust=&limit=` | K 线 + MA5/MA20 |
| GET | `/api/tick?secid=` | 当日分时（含当日资金流曲线） |
| GET | `/api/fundflow?secid=&limit=` | 日级资金流 |
| GET | `/api/holdings?code=&full=` | ETF 持仓（`full=1` 全部持仓） |
| GET | `/api/holdings/quotes?codes=` | 成分股行情（一次请求可拿 500+ 只） |
| GET | `/api/search?q=` | 搜索股票 / 基金 / ETF |
| POST | `/api/watchlist` | `{"action":"add\|remove\|move", ...}` |

`secid` 是「市场.代码」：`1.600519`（沪）、`0.000001`（深）。

---

## 数据来源与**必须知道的限制**

| 数据 | 来源 | 时效 |
| --- | --- | --- |
| 实时行情 | 东方财富 → 新浪 → 腾讯（自动降级） | 实时 |
| K 线 | 东方财富 / Tushare（可选） | 实时 |
| 资金流 | 东方财富 | 实时（接口限流时降级为缓存） |
| **ETF 持仓权重** | 天天基金 F10 | **按基金定期报告披露，滞后 2 周 ~ 9 个月** |
| ETF 成分股行情 | 新浪（单请求批量） | 实时 |

两点必须说清楚，避免误用：

1. **ETF 持仓权重不是实时的**。季报只披露前十大，全部持仓只在半年报/年报里，
   所以「全部持仓」可能旧到半年前。界面顶部会标注报告期。权重当**结构参考**，不能当当日实盘仓位。
2. **接口会限流**。东财对高频请求有冷却（这里做了熔断 + 磁盘缓存兜底），
   被限流时会降级到新浪/腾讯；此时资金流、量比等字段会显示 `--` 而不是误导性的 0。

本项目只用于个人看盘，不构成任何投资建议。

---

## 结构

```
web_server.py            HTTP 服务 + 路由 + 缓存（标准库）
stock_core.py            数据层：多源抓取、降级、熔断、K线/分时/资金流/ETF持仓/搜索
stock_cache.py           磁盘缓存、限速器、指数退避
web/
  index.html
  app.js                 图表与交互（Canvas 手绘，含 PCHIP 平滑、十字光标、缩放）
  style.css
tests/
  test_web_server.py     后端接口测试（真实起服务 + 随机端口 + 断网 SKIP）
  test_web_frontend.mjs  前端逻辑测试（无需浏览器：格式化 / 平滑 / API 路径对齐）
  test_web_render.mjs    前端渲染测试（假 Canvas 上下文：字号、加粗、线宽、涨跌幅文案）
```

---

## 测试

```bash
python tests/test_web_server.py    # 后端：113 项
node   tests/test_web_frontend.mjs # 前端逻辑：24 项（不需要浏览器）
node   tests/test_web_render.mjs   # 前端渲染：20 项（假 Canvas，验证真的画了什么）
```

CI 在每次 push 时跑这两组（见 `.github/workflows/test.yml`）。

---

## License

MIT
