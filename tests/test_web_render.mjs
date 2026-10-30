// 渲染级测试：不需要浏览器。
// 用假的 Canvas 2D 上下文真正执行 drawTick/drawKline/drawFlow，
// 记录每一次 fillText / stroke，从而验证"画面上到底写了什么、多粗、多大"。
// 运行：node tests/test_web_render.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(HERE)
const src = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8')

const fails = []
function check(tag, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${tag}  ${String(extra).slice(0, 150)}`)
  if (!cond) fails.push(tag)
}

// ---------------------------------------------------------------- 假 Canvas
function makeCtx() {
  const rec = { texts: [], strokes: [], fills: [] }
  const ctx = {
    rec,
    font: '10px x', fillStyle: '#000', strokeStyle: '#000', textAlign: 'left',
    textBaseline: 'alphabetic', lineWidth: 1, lineJoin: 'miter',
    save() {}, restore() {}, beginPath() {}, closePath() {}, clearRect() {},
    moveTo() {}, lineTo() {}, setLineDash() {}, arc() {}, fill() {},
    setTransform() {}, translate() {}, scale() {},
    strokeRect() {},
    fillRect(x, y, w, h) { rec.fills.push({ x, y, w, h, fill: ctx.fillStyle }) },
    stroke() { rec.strokes.push({ width: ctx.lineWidth, style: ctx.strokeStyle }) },
    fillText(s, x, y) {
      rec.texts.push({ s: String(s), font: ctx.font, fill: ctx.fillStyle, align: ctx.textAlign, x, y })
    },
    measureText(t) { return { width: String(t).length * 7 } },
  }
  return ctx
}
function makeCanvas(w, h) {
  const ctx = makeCtx()
  return {
    clientWidth: w, clientHeight: h, width: 0, height: 0,
    _ctx: ctx,
    getContext() { return ctx },
    addEventListener() {},
  }
}

// ---------------------------------------------------------------- 载入 app.js
const stubEl = () => ({
  addEventListener() {}, appendChild() {}, style: {}, dataset: {},
  classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, focus() {},
  getBoundingClientRect: () => ({ width: 0, height: 0, left: 0, top: 0 }),
  querySelector: () => stubEl(), querySelectorAll: () => [],
  parentElement: null, children: [], getContext: () => null,
  hidden: false, textContent: '', innerHTML: '', className: '', value: '',
  clientWidth: 600, clientHeight: 300,
})
const doc = {
  querySelector: () => stubEl(), querySelectorAll: () => [],
  createElement: () => stubEl(), addEventListener() {}, body: stubEl(),
}
const noop = () => {}
const win = { devicePixelRatio: 1, addEventListener: noop, innerWidth: 1440, innerHeight: 900 }
const factory = new Function(
  'document', 'window', 'setTimeout', 'clearTimeout', 'setInterval',
  'clearInterval', 'console', 'fetch', 'ResizeObserver', 'confirm', 'alert',
  src + '\n;return {drawTick, drawKline, drawFlow, drawOverlay, S, el, C, fmtPrice};',
)
const M = factory(doc, win, noop, noop, noop, noop, console,
  async () => { throw new Error('no network') }, function () { this.observe = noop }, () => true, noop)

check('app.js 可在假 DOM 下加载', typeof M.drawTick === 'function')

// 把图表画布换成假 canvas
const chart = makeCanvas(900, 600)
const flow = makeCanvas(900, 130)
M.el.chart = chart
M.el.flow = flow

// ---------------------------------------------------------------- 分时（涨幅恰为 0）
const today = '2026-10-30'
const bars = []
let p = 100
for (let i = 1; i <= 240; i++) {
  const hh = i <= 120 ? 9 * 60 + 30 + i : 13 * 60 + (i - 120)
  p += (i % 3 === 0 ? -0.12 : 0.12)
  bars.push({
    t: `${String(Math.floor(hh / 60)).padStart(2, '0')}:${String(hh % 60).padStart(2, '0')}`,
    minute: i, close: i === 240 ? 100.0 : p, avg: 100.0,
    volume: 1000 + (i * 7) % 500, high: 100.3, low: 99.7, open: 100,
  })
}
bars[bars.length - 1].close = 100.0          // 恰好等于昨收 -> 涨幅 0
M.S.selected = '1.600519'
M.S.period = 'tick'
M.S.tickView = null
M.S.tick = {
  name: '测试股', date: today, same_day: true, prev_close: 100.0,
  coverage: 240, bars,
}
M.S.tickFlow = [{ minute: 60, main: 1e7 }, { minute: 120, main: 2e7 }]
M.S.flow = {
  list: [{ date: '2026-10-28', main_net: 1.2e7, super_net: 5e6, big_net: 7e6, mid_net: -2e6, small_net: -1e7, close: 100, pct: 1.2 }],
  stale: false, time: '10:30:00',
}

let threw = null
try { M.drawTick() } catch (e) { threw = e }
check('drawTick 不抛异常', threw === null, threw ? threw.stack.split('\n')[0] : '')

const tTexts = chart._ctx.rec.texts
check('分时画了文字', tTexts.length > 10, `${tTexts.length} 条`)
const priceText = tTexts.find(t => t.s.includes('100.00') && t.s.includes('%'))
check('涨幅为 0 时也显示涨跌幅（+0.00%）', !!priceText && priceText.s.includes('+0.00%'),
  priceText ? priceText.s : '(未找到)')
check('现价+涨跌幅加粗且 18px',
  !!priceText && /^bold 18px /.test(priceText.font),
  priceText ? priceText.font : '')
check('现价文字右对齐', !!priceText && priceText.align === 'right')

const maxStroke = Math.max(...chart._ctx.rec.strokes.map(s => s.width))
check('分时曲线变粗（>= 2.5px）', maxStroke >= 2.5, `最粗 ${maxStroke}px`)
check('分时布局信息完整',
  M.S.layout && M.S.layout.kind === 'tick' && M.S.layout.axisY > 0,
  M.S.layout ? `axisY=${M.S.layout.axisY} plotW=${Math.round(M.S.layout.plotW)}` : '')

// 面板不能压到图表：表头文字 y 必须小于绘图区顶部
const headerY = Math.max(...tTexts.filter(t => t.y < 60).map(t => t.y))
check('表头文字不越界进绘图区', headerY < M.S.layout.mt + 1,
  `表头最低 y=${Math.round(headerY)} 绘图区顶=${M.S.layout.mt}`)

// ---------------------------------------------------------------- K 线
const kbars = []
for (let i = 0; i < 120; i++) {
  const base = 100 + Math.sin(i / 8) * 6
  kbars.push({
    date: `2026-07-${String((i % 28) + 1).padStart(2, '0')}`,
    open: base - 0.5, high: base + 1.2, low: base - 1.4, close: base,
    volume: 10000 + i * 100, amount: 1e9, pct: 0.8,
  })
}
M.S.period = 'daily'
M.S.kline = {
  name: '测试股', period_label: '日K', source: '东财',
  bars: kbars, ma5: kbars.map(b => b.close * 0.99), ma20: kbars.map(b => b.close * 0.97),
}
threw = null
try { M.drawKline() } catch (e) { threw = e }
check('drawKline 不抛异常', threw === null, threw ? threw.stack.split('\n')[0] : '')

const kTexts = chart._ctx.rec.texts
const kPrice = kTexts.filter(t => /%$/.test(t.s) && /^bold 18px/.test(t.font))
check('K 线现价+涨跌幅加粗 18px', kPrice.length > 0, kPrice[0] ? kPrice[0].s : '(未找到)')
const kMaxStroke = Math.max(...chart._ctx.rec.strokes.map(s => s.width))
check('K 线均线加粗（>= 1.5px）', kMaxStroke >= 1.5, `最粗 ${kMaxStroke}px`)
check('K 线布局信息完整', M.S.layout && M.S.layout.kind === 'kline',
  M.S.layout ? `kind=${M.S.layout.kind}` : '')

// ---------------------------------------------------------------- 资金流柱
threw = null
try { M.drawFlow() } catch (e) { threw = e }
check('drawFlow 不抛异常', threw === null, threw ? threw.stack.split('\n')[0] : '')
const fTexts = flow._ctx.rec.texts
check('资金流画了文字', fTexts.length > 0, `${fTexts.length} 条`)
check('资金流显示"最新"', fTexts.some(t => t.s === '最新'))
check('资金流柱有填充', flow._ctx.rec.fills.length > 0, `${flow._ctx.rec.fills.length} 个矩形`)

// ---------------------------------------------------------------- 十字光标叠加
M.S.period = 'tick'
M.drawTick()
M.S.hover = { kind: 'tick', x: (M.S.layout.plotL + M.S.layout.plotR) / 2, y: 300, priceAt: 100.5 }
threw = null
try { M.drawOverlay() } catch (e) { threw = e }
check('十字光标叠加不抛异常', threw === null, threw ? threw.stack.split('\n')[0] : '')
check('十字光标画了价格标签',
  chart._ctx.rec.texts.some(t => t.s === M.fmtPrice(100.5) || /^100\.5/.test(t.s)),
  chart._ctx.rec.texts.slice(-4).map(t => t.s).join('|'))

console.log()
console.log('FAILED:', fails.length, fails)
process.exit(fails.length ? 1 : 0)
