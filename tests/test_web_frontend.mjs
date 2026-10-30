// 前端逻辑测试（不需要浏览器）：
//   1) 把 web/app.js 里的纯函数抽出来在 Node 里跑（格式化 / 平滑 / 刻度）
//   2) 核对 app.js 里用到的每个 /api/* 路径在后端路由表里都存在
// 运行：node tests/test_web_frontend.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(HERE)

const fails = []
function check(tag, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${tag}  ${String(extra).slice(0, 150)}`)
  if (!cond) fails.push(tag)
}

// ---------------------------------------------------------------- 1. 抽出纯函数
const src = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8')

const stubEl = () => ({
  addEventListener() {}, appendChild() {}, style: {}, dataset: {},
  classList: { add() {}, remove() {} }, setAttribute() {}, focus() {},
  getBoundingClientRect: () => ({ width: 0, height: 0, left: 0, top: 0 }),
  querySelector: () => stubEl(), querySelectorAll: () => [],
  parentElement: null, children: [], getContext: () => null,
  hidden: false, textContent: '', innerHTML: '', className: '', value: '',
})
const doc = {
  querySelector: () => stubEl(),
  querySelectorAll: () => [],
  createElement: () => stubEl(),
  addEventListener() {},
  body: stubEl(),
}
const noop = () => {}
const win = { devicePixelRatio: 1, addEventListener: noop, innerWidth: 1440, innerHeight: 900 }
const fakeFetch = async () => { throw new Error('no network in test') }

const exported = [
  'fmtPrice', 'fmtChange', 'fmtPct', 'fmtMoney', 'minuteToClock', 'pchip',
  'niceStep', 'clsOf', 'colorOf', 'escapeHTML', 'text', 'tickHeaderPct',
]
let api
try {
  const factory = new Function(
    'document', 'window', 'setTimeout', 'clearTimeout', 'setInterval',
    'clearInterval', 'console', 'fetch', 'ResizeObserver', 'confirm', 'alert',
    src + `\n;return {${exported.join(',')}};`,
  )
  api = factory(doc, win, noop, noop, noop, noop, console,
    fakeFetch, function () { this.observe = noop }, () => true, noop)
  check('app.js 可在无 DOM 环境加载', true, `导出 ${Object.keys(api).length} 个函数`)
} catch (e) {
  check('app.js 可在无 DOM 环境加载', false, e.message)
  process.exit(1)
}

// ---------------------------------------------------------------- 2. 价格格式（与桌面端一致）
const priceCases = [
  [1283.19, '1,283.19'], [42.4, '42.40'], [9.876, '9.876'],
  [4.38, '4.380'], [0.8765, '0.876'], [12830.5, '12,830.50'],
]
const gotPrices = priceCases.map(([v]) => api.fmtPrice(v))
check('价格规则 <10 三位 / >=10 两位（带千分位）',
  priceCases.every(([v, want], i) => gotPrices[i] === want),
  JSON.stringify(gotPrices))
check('价格为 0 显示 --', api.fmtPrice(0) === '--', api.fmtPrice(0))
check('fmtPct 带符号两位', api.fmtPct(1.234) === '+1.23%' && api.fmtPct(-0.5) === '-0.50%',
  [api.fmtPct(1.234), api.fmtPct(-0.5)])
check('fmtChange 与价格同规则',
  api.fmtChange(19.56) === '+19.56' && api.fmtChange(0.32) === '+0.320',
  [api.fmtChange(19.56), api.fmtChange(0.32)])
check('fmtMoney 亿/万',
  api.fmtMoney(1.2e8) === '1.20亿' && api.fmtMoney(-5e6) === '-500万',
  [api.fmtMoney(1.2e8), api.fmtMoney(-5e6)])
check('涨跌配色：红涨绿跌',
  api.colorOf(1) === '#ff4d4f' && api.colorOf(-1) === '#26c281',
  [api.colorOf(1), api.colorOf(-1)])
check('clsOf 分类正确',
  api.clsOf(2) === 'up' && api.clsOf(-2) === 'down' && api.clsOf(0) === 'flat')

// ---------------------------------------------------------------- 3. 时间轴
const clockCases = [[0, '09:30'], [30, '10:00'], [120, '11:30'], [121, '13:01'], [240, '15:00']]
check('分钟 -> 时钟（含午休跳变）',
  clockCases.every(([m, want]) => api.minuteToClock(m) === want),
  clockCases.map(([m]) => `${m}->${api.minuteToClock(m)}`).join(' '))
check('半点显示秒（半分钟精度）', api.minuteToClock(0.5) === '09:30:30',
  api.minuteToClock(0.5))

// ---------------------------------------------------------------- 4. 平滑（保形，不失真）
const pts = [[0, 0], [10, 10], [20, 0], [30, 10], [40, 0]]
const dense = api.pchip(pts, 4)
check('平滑后点数 = (n-1)*steps+n', dense.length === (pts.length - 1) * 4 + pts.length,
  `${pts.length} -> ${dense.length}`)
const hitsAll = pts.every(([x, y]) => dense.some(([dx, dy]) =>
  Math.abs(dx - x) < 1e-9 && Math.abs(dy - y) < 1e-9))
check('平滑精确经过每个原始点', hitsAll)
let maxOver = 0
for (let i = 0; i < pts.length - 1; i++) {
  const lo = Math.min(pts[i][1], pts[i + 1][1])
  const hi = Math.max(pts[i][1], pts[i + 1][1])
  for (const [x, y] of dense) {
    if (x < pts[i][0] - 1e-9 || x > pts[i + 1][0] + 1e-9) continue
    maxOver = Math.max(maxOver, lo - y, y - hi)
  }
}
check('平滑零过冲（不造假高低点）', maxOver <= 1e-9, `最大越界 ${maxOver.toExponential(2)}`)
check('点太少时原样返回', api.pchip([[0, 1]], 4).length === 1)

// ---------------------------------------------------------------- 4b. 涨跌幅为 0 必须显示（曾经的 bug）
check('涨幅 0 也显示 +0.00%（不再被 falsy 吞掉）',
  api.tickHeaderPct(true, 0) === '+0.00%', api.tickHeaderPct(true, 0))
check('涨幅非 0 正常显示',
  api.tickHeaderPct(true, 1.234) === '+1.23%' && api.tickHeaderPct(true, -0.5) === '-0.50%',
  [api.tickHeaderPct(true, 1.234), api.tickHeaderPct(true, -0.5)])
check('拿不到昨收时才留空', api.tickHeaderPct(false, 0) === '',
  JSON.stringify(api.tickHeaderPct(false, 0)))

// ---------------------------------------------------------------- 4c. 字体大小与加粗
function fakeCtx() {
  return {
    font: '', fillStyle: '', textAlign: '', textBaseline: '',
    save() {}, restore() {}, fillText() {},
  }
}
const ctxA = fakeCtx()
api.text(ctxA, 0, 0, 'x', '#fff', 18, 'right', 'middle', 'bold')
check('可绘制加粗大字（现价+涨跌幅）',
  /^bold 18px /.test(ctxA.font) && ctxA.textAlign === 'right', ctxA.font)
const ctxB = fakeCtx()
api.text(ctxB, 0, 0, 'x', '#fff', 11, 'left')
check('普通文本不加粗', /^11px /.test(ctxB.font), ctxB.font)

const s1 = api.niceStep(2, 4), s2 = api.niceStep(200, 4)
check('niceStep 随跨度放大', s2 > s1 && s1 > 0, `${s1} / ${s2}`)

// ---------------------------------------------------------------- 6. HTML 转义
check('escapeHTML 防注入',
  api.escapeHTML('<img onerror=1>') === '&lt;img onerror=1&gt;',
  api.escapeHTML('<img onerror=1>'))

// ---------------------------------------------------------------- 7. 前后端 API 路径一致性
const backend = fs.readFileSync(path.join(ROOT, 'web_server.py'), 'utf8')
const backendRoutes = new Set(
  [...backend.matchAll(/["'](\/api\/[a-zA-Z0-9_\/]+)["']/g)].map(m => m[1]),
)
const frontendPaths = new Set(
  [...src.matchAll(/\/api\/[a-zA-Z0-9_\/]+/g)].map(m => m[0]),
)
const missing = [...frontendPaths].filter(p => !backendRoutes.has(p))
check('前端用到的每个 API 后端都有', missing.length === 0,
  missing.length ? '缺失: ' + missing.join(', ') : `${frontendPaths.size} 个路径全部匹配`)
check('前端覆盖了核心接口',
  ['/api/status', '/api/watchlist', '/api/quotes', '/api/commodities',
    '/api/kline', '/api/tick', '/api/fundflow', '/api/holdings']
    .every(p => frontendPaths.has(p)),
  [...frontendPaths].sort().join(' '))

// ---------------------------------------------------------------- 8. 静态资源引用
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8')
check('HTML 引用的静态文件都存在',
  ['app.js', 'style.css'].every(f => fs.existsSync(path.join(ROOT, 'web', f))),
  '/static/app.js /static/style.css')
check('HTML 里所有 id 都能在 JS 里找到',
  (() => {
    const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1])
    const missingIds = ids.filter(id => !new RegExp(`['"#]#?${id}['"]|#${id}`).test(src))
    return missingIds.length === 0 ? true : (missingIds.join(',') || false)
  })() !== false)

console.log()
console.log('FAILED:', fails.length, fails)
process.exit(fails.length ? 1 : 0)
