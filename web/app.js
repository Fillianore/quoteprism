/* 股票监控 · 网页版前端
 * 纯原生 JS，无任何外部依赖；图表用 Canvas 2D 手绘。
 */
'use strict';

// ---------------------------------------------------------------- 常量
const C = {
  bg: '#151a21', bgHead: '#10151b', bgAlt: '#1c232c', border: '#263344',
  fg: '#e6edf3', dim: '#8b98a8', faint: '#5b6674',
  up: '#ff4d4f', down: '#26c281', accent: '#3b82f6', warn: '#ffb020',
  grid: '#232c38', ma5: '#e0b53c', ma20: '#a06ee1',
};
const FONT = '"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif';
const PERIODS = [
  ['tick', '分时'], ['5min', '5分'], ['15min', '15分'], ['30min', '30分'],
  ['60min', '60分'], ['daily', '日K'], ['weekly', '周K'], ['monthly', '月K'],
];
const TICK_AXIS_MINUTES = 240;
const TICK_STEPS = [60, 30, 20, 15, 10, 5, 2, 1, 0.5];
const FQT_LABEL = { 0: '不复权', 1: '前复权', 2: '后复权' };

// ---------------------------------------------------------------- 格式化
function fmtPrice(v) {
  const n = Number(v) || 0;
  if (!n) return '--';
  const abs = Math.abs(n);
  if (abs < 10) return n.toFixed(3);
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtChange(v) {
  const n = Number(v) || 0;
  if (!n) return '--';
  const abs = Math.abs(n);
  const s = abs < 10 ? n.toFixed(3) : n.toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return (n > 0 ? '+' : '') + s;
}
function fmtPct(v) {
  const n = Number(v);
  if (!isFinite(n)) return '--';
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}
function fmtMoney(v) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  if (!abs) return '--';
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + '亿';
  if (abs >= 1e4) return (n / 1e4).toFixed(0) + '万';
  return n.toFixed(0);
}
function colorOf(v) {
  const n = Number(v) || 0;
  return n > 0 ? C.up : (n < 0 ? C.down : C.fg);
}
function clsOf(v) {
  const n = Number(v) || 0;
  return n > 0 ? 'up' : (n < 0 ? 'down' : 'flat');
}
/** 分时右上角价格旁的涨跌幅文案。
 *
 * 涨幅恰好为 0 是**合法数值**，必须显示 `+0.00%`；
 * 只有拿不到昨收基准（showPrev=false）时才留空。
 * 不要写成 `dayPct ? ... : ''` —— 0 是 falsy，会把 0 涨幅吞掉（曾经的 bug）。
 */
function tickHeaderPct(showPrev, dayPct) {
  if (!showPrev) return '';
  // 带上符号：0 也显示成 +0.00%，与桌面版一致（那里用 :+.2f）
  const n = Number(dayPct) || 0;
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function minuteToClock(m) {
  const t = Math.max(0, Math.min(TICK_AXIS_MINUTES, Number(m) || 0));
  const total = t <= 120 ? (9 * 60 + 30 + t) : (13 * 60 + (t - 120));
  const hh = Math.floor(total / 60), mm = Math.floor(total % 60);
  let ss = Math.round((total - Math.floor(total)) * 60);
  if (ss >= 60) ss = 0;
  const mm2 = ss ? mm : mm;
  const base = `${String(hh).padStart(2, '0')}:${String(mm2).padStart(2, '0')}`;
  return Math.abs(t - Math.round(t)) < 1e-6 ? base : `${base}:${String(ss).padStart(2, '0')}`;
}
function pchip(points, steps) {
  const n = points.length;
  if (n < 3 || !steps || steps <= 0) return points;
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const h = [], d = [];
  for (let i = 0; i < n - 1; i++) {
    h[i] = xs[i + 1] - xs[i];
    d[i] = h[i] ? (ys[i + 1] - ys[i]) / h[i] : 0;
  }
  const m = new Array(n);
  m[0] = d[0] || 0;
  m[n - 1] = d[n - 2] || 0;
  for (let i = 1; i < n - 1; i++) {
    if (!d[i - 1] || !d[i] || d[i - 1] * d[i] <= 0) m[i] = 0;
    else {
      const w1 = 2 * h[i] + h[i - 1], w2 = h[i] + 2 * h[i - 1];
      m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
    }
  }
  for (let i = 0; i < n - 1; i++) {
    if (!d[i]) { m[i] = 0; m[i + 1] = 0; continue; }
    let a = m[i] / d[i], b = m[i + 1] / d[i];
    if (a < 0) { a = 0; m[i] = 0; }
    if (b < 0) { b = 0; m[i + 1] = 0; }
    const s = a * a + b * b;
    if (s > 9) {
      const t = 3 / Math.sqrt(s);
      m[i] = t * a * d[i];
      m[i + 1] = t * b * d[i];
    }
  }
  const out = [];
  for (let i = 0; i < n - 1; i++) {
    out.push([xs[i], ys[i]]);
    const lo = Math.min(ys[i], ys[i + 1]), hi = Math.max(ys[i], ys[i + 1]);
    for (let k = 1; k <= steps; k++) {
      const t = k / (steps + 1), t2 = t * t, t3 = t2 * t;
      const y = (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
        + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1];
      out.push([xs[i] + t * h[i], Math.min(hi, Math.max(lo, y))]);
    }
  }
  out.push([xs[n - 1], ys[n - 1]]);
  return out;
}

// ---------------------------------------------------------------- API
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ cache: 'no-store' }, opts || {}));
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const data = await res.json();
  if (data && data.ok === false && data.error) throw new Error(data.error);
  return data;
}
async function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// ---------------------------------------------------------------- 状态
const S = {
  watch: [], quotes: {}, selected: null,
  period: 'tick', adjust: 1, viewBars: 90,
  kline: null, tick: null, flow: null, commo: [],
  holdings: null, holdingsFull: false, holdingsSort: 'weight', holdingsDesc: true,
  tickView: null, sortCol: 'default', sortDesc: false,
  phase: '', trading: false, source: '', status: null,
  refreshSec: 3, idleSec: 60,
  hover: null, loading: false,
};

const $ = s => document.querySelector(s);
const el = {
  phase: $('#phase'), conn: $('#conn'), stamp: $('#stamp'), ver: $('#ver'),
  commo: $('#commo'), wlBody: $('#wl-body'), sideCount: $('#side-count'),
  addInput: $('#add-input'), btnAdd: $('#btn-add'), btnUp: $('#btn-up'),
  btnDown: $('#btn-down'), btnDel: $('#btn-del'), btnRefresh: $('#btn-refresh'),
  periods: $('#periods'), btnAdjust: $('#btn-adjust'), btnHoldings: $('#btn-holdings'),
  chart: $('#chart'), tip: $('#tip'), flow: $('#flow'), flowTitle: $('#flow-title'),
  flowNote: $('#flow-note'), holdingsBox: $('#holdings-box'),
  holdingsTitle: $('#holdings-title'), holdingsBody: $('#holdings-body'),
  btnHoldingsMode: $('#btn-holdings-mode'), statusLeft: $('#status-left'),
  statusRight: $('#status-right'), chartHint: $('#chart-hint'),
};

// ---------------------------------------------------------------- 画布
function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(100, canvas.clientWidth), h = Math.max(80, canvas.clientHeight);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}
function line(ctx, x1, y1, x2, y2, color, width, dash) {
  ctx.save();
  ctx.strokeStyle = color; ctx.lineWidth = width || 1;
  ctx.setLineDash(dash || []);
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  ctx.restore();
}
function text(ctx, x, y, str, color, size, align, base, weight) {
  ctx.save();
  ctx.fillStyle = color;
  ctx.font = `${weight ? weight + ' ' : ''}${size || 11}px ${FONT}`;
  ctx.textAlign = align || 'left';
  ctx.textBaseline = base || 'middle';
  ctx.fillText(str, x, y);
  ctx.restore();
}
function niceStep(span, target) {
  if (!isFinite(span) || span <= 0) return 1;
  const raw = span / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * mag;
}

// ---------------------------------------------------------------- 分时图
function drawTick() {
  const { ctx, w, h } = fitCanvas(el.chart);
  ctx.clearRect(0, 0, w, h);
  const d = S.tick;
  if (!d || !d.bars || !d.bars.length) {
    text(ctx, w / 2, h / 2 - 8, '暂无当日分时数据', C.fg, 14, 'center');
    text(ctx, w / 2, h / 2 + 14, '该股今日可能停牌或全天无成交；可切换「日K」查看历史', C.dim, 11, 'center');
    S.layout = null; return;
  }
  const padL = 8, padR = 70, infoH = 48, axisBand = 26;
  const volH = Math.max(50, Math.round(h * 0.16));
  const flowH = (S.tickFlow && S.tickFlow.length) ? Math.round(h * 0.2) : 0;
  const flowLabel = 16;
  const mt = infoH, mb = h - volH - axisBand - flowLabel - flowH - 8;
  const vt = mb + axisBand, vb = vt + volH;
  const plotL = padL, plotR = w - padR, plotW = Math.max(50, plotR - plotL);

  const [v0, v1] = S.tickView || [0, TICK_AXIS_MINUTES];
  const vspan = Math.max(1, v1 - v0);
  const xOf = m => plotL + (m - v0) / vspan * plotW;

  const visible = d.bars.filter(b => b.minute >= v0 - 2 && b.minute <= v1 + 2);
  if (!visible.length) { text(ctx, w / 2, h / 2, '该区间无数据', C.dim, 12, 'center'); return; }
  const closes = visible.map(b => b.close).filter(v => v);
  let hi = Math.max.apply(null, closes), lo = Math.min.apply(null, closes);
  const usePrev = d.same_day && d.prev_close > 0;
  if (usePrev) {
    const mid = (hi + lo) / 2;
    if (mid > 0 && d.prev_close / mid >= 0.5 && d.prev_close / mid <= 2) {
      hi = Math.max(hi, d.prev_close); lo = Math.min(lo, d.prev_close);
    }
  }
  if (hi <= lo) { hi = lo + 1; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
  const span = hi - lo;
  const yOf = p => mb - (p - lo) / span * (mb - mt);

  // 网格 + 价格刻度
  let step = niceStep(span, 4), t = Math.ceil(lo / step) * step;
  ctx.save();
  while (t < hi) {
    const y = yOf(t);
    line(ctx, plotL, y, plotR, y, C.grid);
    text(ctx, plotR + 6, y, fmtPrice(t), C.dim, 11, 'left');
    t += step;
  }
  ctx.restore();

  // 时间刻度
  const ticks = [];
  {
    const minGap = 88;
    let s = TICK_STEPS[0];
    for (const cand of TICK_STEPS) if (cand / vspan * plotW >= minGap) s = cand;
    for (let m = Math.ceil(v0 / s) * s; m <= v1 + 1e-9; m += s) ticks.push(m);
  }
  for (const m of ticks) line(ctx, xOf(m), mt, xOf(m), vb, C.grid, 1, [2, 4]);

  // 边框
  ctx.save();
  ctx.strokeStyle = '#2b3542'; ctx.lineWidth = 1;
  ctx.strokeRect(plotL + .5, mt + .5, plotW - 1, mb - mt - 1);
  ctx.strokeRect(plotL + .5, vt + .5, plotW - 1, volH - 1);
  ctx.restore();

  // 昨收线
  let showPrev = false;
  if (usePrev && lo < d.prev_close && d.prev_close < hi) {
    const mid = (Math.max.apply(null, closes) + Math.min.apply(null, closes)) / 2;
    if (mid > 0 && d.prev_close / mid >= 0.5 && d.prev_close / mid <= 2) {
      showPrev = true;
      const y = yOf(d.prev_close);
      line(ctx, plotL, y, plotR, y, '#6b7787', 1, [4, 3]);
      text(ctx, plotR - 4, y - 7, '昨收 ' + fmtPrice(d.prev_close), '#8b98a8', 11, 'right');
    }
  }

  // 价格 / 均价 曲线（分段 + 平滑）
  const priceColor = colorOf(usePrev ? (d.bars[d.bars.length - 1].close - d.prev_close) : 0);
  const segs = [], avgSegs = [];
  let cur = [], avgCur = [], prevM = null;
  for (const b of visible) {
    if (prevM !== null && b.minute - prevM > 3) {
      if (cur.length) segs.push(cur);
      if (avgCur.length) avgSegs.push(avgCur);
      cur = []; avgCur = [];
    }
    cur.push([xOf(b.minute), yOf(b.close)]);
    if (b.avg) avgCur.push([xOf(b.minute), yOf(b.avg)]);
    prevM = b.minute;
  }
  if (cur.length) segs.push(cur);
  if (avgCur.length) avgSegs.push(avgCur);

  const stroke = (pts, color, width) => {
    if (pts.length < 2) return;
    const dense = pchip(pts, 4);
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = width || 1.5;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    dense.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
    ctx.restore();
  };
  avgSegs.forEach(s => stroke(s, C.ma5, 1.5));
  segs.forEach(s => stroke(s, priceColor, 2.6));

  // 成交量
  const maxVol = Math.max.apply(null, visible.map(b => b.volume || 0)) || 1;
  const bw = Math.max(1, plotW / vspan * 0.8);
  visible.forEach((b, i) => {
    const x = xOf(b.minute);
    const hh = Math.max(1, (b.volume || 0) / maxVol * (volH - 4));
    const prev = i ? visible[i - 1] : null;
    const up = b.close >= (prev ? prev.close : b.open || b.close);
    ctx.fillStyle = up ? C.up : C.down;
    ctx.fillRect(x - bw / 2, vb - hh, bw, hh);
  });
  text(ctx, plotL + 2, vt + 8, '成交量 最大 ' + fmtMoney(maxVol) + '手', C.dim, 11, 'left');

  // 时间轴
  const axisY = mb + axisBand / 2;
  for (const m of ticks) {
    line(ctx, xOf(m), mb, xOf(m), mb + 4, '#3a4656');
    const label = Math.abs(m - 120) < 1e-9 ? '11:30/13:00' : minuteToClock(m);
    text(ctx, xOf(m), axisY + 2, label, '#8b98a8', 11, 'center');
  }

  // 当日资金流
  const flows = S.tickFlow || [];
  if (flows.length) {
    const fTop = vb + flowLabel;
    const fMid = (fTop + h - 6) / 2, fHalf = (h - 6 - fTop) / 2;
    line(ctx, plotL, fMid, plotR, fMid, C.grid);
    const peak = Math.max.apply(null, flows.map(f => Math.abs(f.main) || 0)) || 1;
    const fp = flows.filter(f => f.minute >= v0 - 2 && f.minute <= v1 + 2)
      .map(f => [xOf(f.minute), fMid - (f.main || 0) / peak * fHalf]);
    if (fp.length > 1) {
      const last = flows[flows.length - 1].main || 0;
      stroke(fp, last >= 0 ? C.up : C.down, 1.5);
    }
    text(ctx, plotL + 2, fTop + 2, '当日资金流 主力 ' + fmtMoney(flows[flows.length - 1].main),
      C.dim, 11, 'left');
  } else {
    text(ctx, plotL + 2, vb + 14, '⚠ 当日资金流分时不可用（接口限流），下方为日级资金流', C.warn, 11, 'left');
  }

  // 信息区
  const last = d.bars[d.bars.length - 1];
  const dayPct = showPrev && d.prev_close ? (last.close - d.prev_close) / d.prev_close * 100 : 0;
  let title = (d.name || '') + '  分时';
  if (!d.same_day) title += `（${d.date} 数据）`;
  if (S.tickView) title += `　${minuteToClock(v0)}–${minuteToClock(v1)}`;
  text(ctx, plotL + 2, 14, title, d.same_day ? C.fg : C.warn, 14, 'left', 'middle', 'bold');
  text(ctx, plotL + 2, 32,
    `现价 ${fmtPrice(last.close)}  最高 ${fmtPrice(Math.max.apply(null, closes))}  `
    + `最低 ${fmtPrice(Math.min.apply(null, closes))}  覆盖 ${d.coverage}/240 分钟`,
    C.dim, 12, 'left');
  // 注意：涨幅为 0 也必须显示（0 是合法数值，不能当成"没有数据"）
  text(ctx, plotR, 14, `${fmtPrice(last.close)}  ${tickHeaderPct(showPrev, dayPct)}`,
    showPrev ? colorOf(dayPct) : C.fg, 18, 'right', 'middle', 'bold');
  text(ctx, plotL + 330, 14, '— 价格', priceColor, 11, 'left');
  text(ctx, plotL + 392, 14, '— 均价', C.ma5, 11, 'left');

  S.layout = {
    kind: 'tick', plotL, plotR, plotW, mt, mb, vt, vb, lo, hi, v0, v1, vspan,
    xOf, yOf, visible, prevClose: d.prev_close, showPrev, priceColor, axisY,
  };
}

// ---------------------------------------------------------------- K 线图
function drawKline() {
  const { ctx, w, h } = fitCanvas(el.chart);
  ctx.clearRect(0, 0, w, h);
  const d = S.kline;
  const bars = d && d.bars ? d.bars : [];
  if (!bars.length) {
    text(ctx, w / 2, h / 2, '暂无K线数据', C.dim, 14, 'center');
    S.layout = null; return;
  }
  const n = Math.min(S.viewBars, bars.length);
  const end = Math.max(n, bars.length);
  const start = Math.max(0, end - n);
  const vis = bars.slice(start, end);
  const ma5 = (d.ma5 || []).slice(start, end);
  const ma20 = (d.ma20 || []).slice(start, end);

  const padL = 8, padR = 70, infoH = 48, axisH = 22;
  const volH = Math.max(50, Math.round(h * 0.2));
  const mt = infoH, mb = h - volH - axisH - 8;
  const vt = mb + 10, vb = h - axisH - 4;
  const plotL = padL, plotR = w - padR, plotW = Math.max(50, plotR - plotL);
  const bw = plotW / vis.length;

  const highs = vis.map(b => b.high || b.close), lows = vis.map(b => b.low || b.close);
  let hi = Math.max.apply(null, highs), lo = Math.min.apply(null, lows);
  if (hi <= lo) { hi = lo + 1; lo -= 0.5; }
  const pad = (hi - lo) * 0.06; hi += pad; lo -= pad;
  const span = hi - lo;
  const yOf = p => mb - (p - lo) / span * (mb - mt);
  const xOf = i => plotL + (i + 0.5) * bw;

  let step = niceStep(span, 5), t = Math.ceil(lo / step) * step;
  while (t < hi) {
    const y = yOf(t);
    line(ctx, plotL, y, plotR, y, C.grid);
    text(ctx, plotR + 6, y, fmtPrice(t), C.dim, 10, 'left');
    t += step;
  }
  // 昨收（用首个 bar 的开盘，与桌面版一致）
  const prev = vis[0] ? (vis[0].open || 0) : 0;
  if (prev > lo && prev < hi) {
    const y = yOf(prev);
    line(ctx, plotL, y, plotR, y, '#3a4656', 1, [3, 3]);
    text(ctx, plotR - 4, y - 7, '昨收 ' + fmtPrice(prev), '#6c7a8c', 10, 'right');
  }

  // K 线
  const bodyW = Math.max(1, Math.min(bw * 0.68, 14));
  vis.forEach((b, i) => {
    const x = xOf(i);
    const up = (b.close || 0) >= (b.open || 0);
    ctx.strokeStyle = up ? C.up : C.down;
    ctx.fillStyle = up ? C.up : C.down;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yOf(b.high || b.close));
    ctx.lineTo(x, yOf(b.low || b.close)); ctx.stroke();
    const y1 = yOf(b.open || b.close), y2 = yOf(b.close || 0);
    const top = Math.min(y1, y2), hgt = Math.max(1, Math.abs(y2 - y1));
    ctx.fillRect(x - bodyW / 2, top, bodyW, hgt);
  });

  // 成交量
  const maxVol = Math.max.apply(null, vis.map(b => b.volume || 0)) || 1;
  vis.forEach((b, i) => {
    const x = xOf(i);
    const hh = Math.max(1, (b.volume || 0) / maxVol * (vb - vt));
    const up = (b.close || 0) >= (b.open || 0);
    ctx.fillStyle = up ? C.up : C.down;
    ctx.fillRect(x - bodyW / 2, vb - hh, bodyW, hh);
  });
  text(ctx, plotL + 2, vt - 6, '成交量 最大 ' + fmtMoney(maxVol) + '手', C.dim, 11, 'left');

  // 均线
  const drawMA = (arr, color) => {
    const pts = [];
    arr.forEach((v, i) => { if (v) pts.push([xOf(i), yOf(v)]); });
    if (pts.length < 2) return;
    ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke(); ctx.restore();
  };
  drawMA(ma5, C.ma5);
  drawMA(ma20, C.ma20);

  // 横轴日期
  const slots = Math.max(2, Math.floor(plotW / 78));
  for (let k = 0; k <= slots; k++) {
    const idx = Math.min(vis.length - 1, Math.round(k * (vis.length - 1) / slots));
    const x = xOf(idx);
    line(ctx, x, mt, x, vb, '#1b232e');
    text(ctx, x, vb + 10, String(vis[idx].date || '').slice(5), '#6c7a8c', 11, 'center');
  }

  const last = vis[vis.length - 1];
  const pct = Number(last.pct) || 0;
  let title = `${d.name || ''}  ${d.period_label || ''}`;
  if (d.source && !/东财/.test(d.source)) title += `  源:${d.source}`;
  text(ctx, plotL + 2, 14, title, C.fg, 14, 'left', 'middle', 'bold');
  text(ctx, plotL + 2, 30,
    `开 ${fmtPrice(last.open)}  高 ${fmtPrice(last.high)}  低 ${fmtPrice(last.low)}  `
    + `收 ${fmtPrice(last.close)}`, C.dim, 12, 'left');
  text(ctx, plotR, 14, `${fmtPrice(last.close)}  ${fmtPct(pct)}`, colorOf(pct), 18, 'right', 'middle', 'bold');
  text(ctx, plotR, 30, `量 ${fmtMoney(last.volume)}手`, C.dim, 11, 'right');
  text(ctx, plotL + 330, 14, '— MA5', C.ma5, 11, 'left');
  text(ctx, plotL + 392, 14, '— MA20', C.ma20, 11, 'left');

  S.layout = { kind: 'kline', plotL, plotR, plotW, mt, mb, vt, vb, lo, hi, bw, vis, ma5, ma20,
               xOf, yOf, prev, axisY: vb + 10 };
}

// ---------------------------------------------------------------- 资金流柱
function drawFlow() {
  const { ctx, w, h } = fitCanvas(el.flow);
  ctx.clearRect(0, 0, w, h);
  const list = (S.flow && S.flow.list) || [];
  el.flowTitle.textContent = '主力净流入（近 20 日）';
  if (!list.length) {
    text(ctx, w / 2, h / 2, '暂无资金流数据', C.dim, 12, 'center');
    S.flowLayout = null; return;
  }
  const padL = 8, padR = 84, top = 24, bottom = h - 18;
  const plotL = padL, plotR = w - padR, plotW = Math.max(40, plotR - plotL);
  const peak = Math.max.apply(null, list.map(b => Math.abs(b.main_net || 0))) || 1;
  const mid = (top + bottom) / 2, half = (bottom - top) / 2;
  line(ctx, plotL, mid, plotR, mid, C.grid);
  const bw = plotW / list.length, bodyW = Math.max(2, Math.min(bw * 0.62, 20));
  list.forEach((b, i) => {
    const x = plotL + (i + 0.5) * bw;
    const hh = Math.abs(b.main_net || 0) / peak * half;
    ctx.fillStyle = (b.main_net || 0) >= 0 ? C.up : C.down;
    ctx.fillRect(x - bodyW / 2, (b.main_net || 0) >= 0 ? mid - hh : mid, bodyW, Math.max(1, hh));
  });
  text(ctx, plotR + 6, mid - half + 6, fmtMoney(peak), C.dim, 11, 'left');
  text(ctx, plotR + 6, mid + half - 6, fmtMoney(-peak), C.dim, 11, 'left');
  const last = list[list.length - 1];
  text(ctx, plotR + 6, mid, '最新', C.dim, 11, 'left');
  text(ctx, plotR + 6, mid + 12, fmtMoney(last.main_net), colorOf(last.main_net), 12, 'left', 'middle', 'bold');
  [0, list.length - 1].forEach(i => {
    const x = plotL + (i + 0.5) * bw;
    text(ctx, x, h - 8, String(list[i].date || '').slice(5), '#6c7a8c', 10,
      i === 0 ? 'left' : 'right');
  });
  el.flowNote.textContent = (S.flow.stale ? `⚠ 接口限流，以下为缓存 · ${S.flow.time || ''}` : '');
  S.flowLayout = { plotL, plotR, plotW, bw, top, bottom, mid, half, list };
}

// ---------------------------------------------------------------- 十字光标
function showTip(lines, x, y) {
  el.tip.hidden = false;
  el.tip.innerHTML = lines.map((l, i) => (i === 0 ? `<b>${escapeHTML(l)}</b>` : escapeHTML(l))).join('\n');
  const box = el.tip.getBoundingClientRect();
  const host = el.chart.parentElement.getBoundingClientRect();
  let left = x + 14, top = y - box.height - 12;
  if (left + box.width > host.width - 4) left = x - box.width - 14;
  if (top < 2) top = y + 14;
  el.tip.style.left = Math.max(2, Math.min(left, host.width - box.width - 4)) + 'px';
  el.tip.style.top = Math.max(2, Math.min(top, host.height - box.height - 4)) + 'px';
}
function hideTip() { el.tip.hidden = true; }
function escapeHTML(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function chartMouse(ev) {
  const lay = S.layout;
  if (!lay) return;
  const rect = el.chart.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  if (x < lay.plotL - 2 || x > lay.plotR + 2) { hideTip(); S.hover = null; draw(); return; }

  if (lay.kind === 'tick') {
    const m = lay.v0 + (x - lay.plotL) / lay.plotW * lay.vspan;
    let best = null;
    for (const b of lay.visible) {
      if (!best || Math.abs(b.minute - m) < Math.abs(best.minute - m)) best = b;
    }
    if (!best) return;
    const xs = lay.xOf(best.minute);
    const yc = Math.min(Math.max(y, lay.mt), lay.mb);
    const priceAt = lay.lo + (lay.mb - yc) / Math.max(1, lay.mb - lay.mt) * (lay.hi - lay.lo);
    const pct = lay.showPrev && lay.prevClose
      ? (best.close - lay.prevClose) / lay.prevClose * 100 : 0;
    showTip([
      `${best.t}   价 ${fmtPrice(best.close)}   ${fmtPct(pct)}`,
      `均价 ${fmtPrice(best.avg)}   量 ${fmtMoney(best.volume)}手`,
      `高 ${fmtPrice(best.high)}  低 ${fmtPrice(best.low)}  光标 ${fmtPrice(priceAt)}`,
    ], xs, yc);
    S.hover = { kind: 'tick', x: xs, y: yc, priceAt };
  } else {
    let idx = Math.floor((x - lay.plotL) / lay.bw);
    idx = Math.max(0, Math.min(lay.vis.length - 1, idx));
    const b = lay.vis[idx];
    const xs = lay.xOf(idx);
    const yc = Math.min(Math.max(y, lay.mt), lay.mb);
    const priceAt = lay.lo + (lay.mb - yc) / Math.max(1, lay.mb - lay.mt) * (lay.hi - lay.lo);
    const pct = lay.prev ? (b.close - lay.prev) / lay.prev * 100 : (Number(b.pct) || 0);
    showTip([
      `${b.date}   收 ${fmtPrice(b.close)}   ${fmtPct(pct)}`,
      `开 ${fmtPrice(b.open)}  高 ${fmtPrice(b.high)}  低 ${fmtPrice(b.low)}`,
      `量 ${fmtMoney(b.volume)}手   额 ${fmtMoney(b.amount)}`,
      `MA5 ${fmtPrice((lay.ma5 || [])[idx])}   MA20 ${fmtPrice((lay.ma20 || [])[idx])}`,
    ], xs, yc);
    S.hover = { kind: 'kline', x: xs, y: yc, priceAt, idx };
  }
  drawOverlay();
}

function drawOverlay() {
  const lay = S.layout, hv = S.hover;
  if (!lay || !hv) return;
  const ctx = el.chart.getContext('2d');
  // 重画主体后再叠加（简单做法：整幅重绘 + 叠加）
  draw();
  const g = el.chart.getContext('2d');
  line(g, hv.x, lay.mt, hv.x, lay.vb, '#8b98a8', 1, [3, 3]);
  line(g, lay.plotL, hv.y, lay.plotR, hv.y, '#8b98a8', 1, [3, 3]);
  // 右侧价格标签
  const label = fmtPrice(hv.priceAt);
  g.save();
  g.fillStyle = '#2b3542';
  g.fillRect(lay.plotR + 2, hv.y - 10, 64, 20);
  g.restore();
  text(g, lay.plotR + 34, hv.y, label, '#e6edf3', 11, 'center', 'middle', 'bold');
  // 底部时间标签
  let tlabel = '';
  if (lay.kind === 'tick') {
    const b = lay.visible.find(v => Math.abs(lay.xOf(v.minute) - hv.x) < 0.6);
    tlabel = b ? minuteToClock(b.minute) : '';
  } else {
    const b = lay.vis[hv.idx];
    tlabel = b ? String(b.date) : '';
  }
  if (tlabel) {
    const tw = Math.max(58, tlabel.length * 8);
    g.save(); g.fillStyle = '#2b3542';
    g.fillRect(Math.min(Math.max(lay.plotL + tw / 2, hv.x), lay.plotR - tw / 2) - tw / 2,
      lay.axisY - 10, tw, 20);
    g.restore();
    text(g, Math.min(Math.max(lay.plotL + tw / 2, hv.x), lay.plotR - tw / 2), lay.axisY,
      tlabel, '#e6edf3', 11, 'center', 'middle', 'bold');
  }
}

// ---------------------------------------------------------------- 选股 / 自选
function renderWatch() {
  const rows = S.watch.slice();
  if (S.sortCol !== 'default') {
    const key = S.sortCol;
    rows.sort((a, b) => {
      const qa = S.quotes[a.secid] || {}, qb = S.quotes[b.secid] || {};
      const va = key === 'name' ? a.name : (key === 'code' ? a.code : (qa[key] || 0));
      const vb = key === 'name' ? b.name : (key === 'code' ? b.code : (qb[key] || 0));
      const r = typeof va === 'string' ? String(va).localeCompare(String(vb), 'zh') : (va - vb);
      return S.sortDesc ? -r : r;
    });
  }
  el.wlBody.innerHTML = '';
  for (const s of rows) {
    const q = S.quotes[s.secid] || {};
    const tr = document.createElement('tr');
    if (s.secid === S.selected) tr.className = 'sel';
    tr.dataset.secid = s.secid;
    const pctCls = clsOf(q.pct);
    tr.innerHTML = `
      <td title="${escapeHTML(s.name || '')}"><div class="nm">${escapeHTML(s.name || '')}</div></td>
      <td class="hide-sm faint">${escapeHTML(s.code || '')}</td>
      <td class="num ${pctCls}">${fmtPrice(q.price)}</td>
      <td class="num hide-sm ${pctCls}">${fmtChange(q.change)}</td>
      <td class="num ${pctCls}">${fmtPct(q.pct)}</td>
      <td class="num hide-sm ${clsOf(q.main_net)}">${fmtMoney(q.main_net)}</td>`;
    tr.addEventListener('click', () => selectStock(s.secid));
    el.wlBody.appendChild(tr);
  }
  el.sideCount.textContent = `${S.watch.length} 只`;
}

function selectStock(secid) {
  if (!secid || secid === S.selected) return;
  S.selected = secid;
  S.kline = S.tick = S.flow = S.tickFlow = null;
  S.tickView = null;
  S.holdings = null;
  renderWatch();
  const s = S.watch.find(x => x.secid === secid);
  const isEtf = s && /^(51|56|58|15|16)/.test(s.code || '');
  el.btnHoldings.hidden = !isEtf;
  el.holdingsBox.hidden = true;
  hideTip();
  S.hover = null;
  draw();
  loadDetail();
}

function renderPeriods() {
  el.periods.innerHTML = '';
  for (const [key, label] of PERIODS) {
    const b = document.createElement('button');
    b.className = 'btn ghost' + (S.period === key ? ' active' : '');
    b.textContent = label;
    b.dataset.p = key;
    b.addEventListener('click', () => setPeriod(key));
    el.periods.appendChild(b);
  }
  el.btnAdjust.textContent = FQT_LABEL[S.adjust] || '前复权';
}

function setPeriod(p) {
  if (S.period === p) return;
  S.period = p;
  S.tickView = null;
  S.hover = null;
  hideTip();
  renderPeriods();
  draw();
  loadDetail();
}

function draw() {
  if (S.period === 'tick') drawTick(); else drawKline();
}

// ---------------------------------------------------------------- 数据加载
function drawSafe() {
  try { draw(); } catch (e) { console.error(e); }
}

async function loadStatus() {
  try {
    const d = await api('/api/status');
    S.status = d; S.phase = d.phase; S.trading = d.trading;
    S.refreshSec = d.refresh_sec || 3; S.idleSec = d.idle_refresh_sec || 60;
    el.phase.textContent = d.phase || '—';
    el.phase.className = 'badge' + (d.trading ? ' live' : '');
    el.ver.textContent = 'v' + (d.version || '');
  } catch (e) { /* 忽略 */ }
}

async function loadWatchlist() {
  const d = await api('/api/watchlist');
  S.watch = d.list || [];
  if (!S.selected || !S.watch.find(x => x.secid === S.selected)) {
    S.selected = S.watch.length ? S.watch[0].secid : null;
  }
  const s = S.watch.find(x => x.secid === S.selected);
  const isEtf = s && /^(51|56|58|15|16)/.test(s.code || '');
  el.btnHoldings.hidden = !isEtf;
  renderWatch();
}

async function loadQuotes() {
  try {
    const d = await api('/api/quotes');
    S.source = d.source || '';
    const map = {};
    for (const q of (d.list || [])) map[q.secid] = q;
    S.quotes = map;
    el.conn.textContent = `来源 ${d.source || '—'}`;
    el.conn.className = 'conn';
    renderWatch();
    for (const s of S.watch) {
      const q = map[s.secid];
      if (q && q.name && q.name !== s.name) s.name = q.name;
    }
    const now = new Date();
    el.stamp.textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
  } catch (e) {
    el.conn.textContent = '行情获取失败：' + e.message;
    el.conn.className = 'conn err';
  }
}

async function loadCommo() {
  try {
    const d = await api('/api/commodities');
    S.commo = d.list || [];
    if (!S.commo.length) { el.commo.hidden = true; return; }
    el.commo.hidden = false;
    el.commo.innerHTML = S.commo.map(c => {
      const cls = c.stale ? 'stale' : clsOf(c.pct);
      return `<span class="item"><span class="n">${escapeHTML(c.name)}</span>`
        + `<span class="v ${cls}">${fmtPrice(c.price)} ${fmtPct(c.pct)}</span></span>`;
    }).join('');
  } catch (e) { el.commo.hidden = true; }
}

async function loadDetail() {
  if (!S.selected) return;
  const secid = S.selected;
  try {
    if (S.period === 'tick') {
      const d = await api('/api/tick?secid=' + encodeURIComponent(secid));
      if (secid !== S.selected) return;
      S.tick = d;
      S.tickFlow = d.flow || [];
      try {
        const f = await api('/api/fundflow?secid=' + encodeURIComponent(secid) + '&limit=20');
        S.flow = f;
      } catch (e) { S.flow = { list: [] }; }
      drawSafe();
      drawFlow();
    } else {
      const d = await api(`/api/kline?secid=${encodeURIComponent(secid)}`
        + `&period=${S.period}&adjust=${S.adjust}&limit=400`);
      if (secid !== S.selected) return;
      S.kline = d;
      drawSafe();
      try {
        const f = await api('/api/fundflow?secid=' + encodeURIComponent(secid) + '&limit=20');
        if (secid === S.selected) { S.flow = f; drawFlow(); }
      } catch (e) { /* 忽略 */ }
    }
    el.statusLeft.textContent =
      `更新 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })} · ${S.phase}`
      + ` · 每 ${S.trading ? S.refreshSec : S.idleSec}s 刷新`;
    el.statusRight.textContent = S.watch.find(x => x.secid === secid)?.name || '';
  } catch (e) {
    el.statusLeft.textContent = '数据获取失败：' + e.message;
  }
}

async function loadHoldings() {
  const s = S.watch.find(x => x.secid === S.selected);
  if (!s) return;
  try {
    const d = await api(`/api/holdings?code=${encodeURIComponent(s.code)}`
      + `&full=${S.holdingsFull ? 1 : 0}`);
    S.holdings = d;
    el.holdingsBox.hidden = false;
    el.holdingsTitle.textContent = `${d.period || ''} · ${d.kind === 'full' ? '全部持仓' : '前十大'}`
      + ` · ${d.items.length} 只 · 合计 ${Number(d.total_weight || 0).toFixed(2)}%`;
    el.btnHoldingsMode.textContent = S.holdingsFull ? '前十大' : '全部持仓';
    await loadHoldingsQuotes();
  } catch (e) {
    el.holdingsBox.hidden = false;
    el.holdingsTitle.textContent = '持仓加载失败：' + e.message;
    el.holdingsBody.innerHTML = '';
  }
}

async function loadHoldingsQuotes() {
  const d = S.holdings;
  if (!d || !d.items.length) return;
  const codes = d.items.map(i => i.code).join(',');
  try {
    const q = await api('/api/holdings/quotes?codes=' + encodeURIComponent(codes));
    S.holdingsQuotes = q.quotes || {};
  } catch (e) { S.holdingsQuotes = S.holdingsQuotes || {}; }
  renderHoldings();
}

function renderHoldings() {
  const d = S.holdings;
  if (!d) return;
  const qs = S.holdingsQuotes || {};
  const items = d.items.map(it => {
    const q = qs[it.code] || {};
    return Object.assign({}, it, {
      price: q.price, change: q.change, pct: q.pct,
      contrib: q.pct ? it.weight * q.pct / 100 : null,
    });
  });
  const key = S.holdingsSort;
  items.sort((a, b) => {
    const va = a[key], vb = b[key];
    if (typeof va === 'string' || typeof vb === 'string') {
      return (S.holdingsDesc ? -1 : 1) * String(va).localeCompare(String(vb), 'zh');
    }
    return (S.holdingsDesc ? -1 : 1) * ((Number(vb) || 0) - (Number(va) || 0));
  });
  el.holdingsBody.innerHTML = items.slice(0, 400).map(it => `
    <tr>
      <td>${escapeHTML(it.name || '')}</td>
      <td class="faint">${escapeHTML(it.code || '')}</td>
      <td class="num">${Number(it.weight || 0).toFixed(2)}%</td>
      <td class="num ${clsOf(it.pct)}">${fmtPrice(it.price)}</td>
      <td class="num ${clsOf(it.pct)}">${fmtChange(it.change)}</td>
      <td class="num ${clsOf(it.pct)}">${it.pct == null ? '--' : fmtPct(it.pct)}</td>
      <td class="num ${clsOf(it.contrib)}">${it.contrib == null ? '--' : fmtPct(it.contrib)}</td>
    </tr>`).join('');
}

// ---------------------------------------------------------------- 交互
function bind() {
  el.btnAdjust.addEventListener('click', () => {
    S.adjust = (S.adjust + 1) % 3;
    renderPeriods();
    S.kline = null;
    if (S.period !== 'tick') loadDetail();
  });
  el.btnRefresh.addEventListener('click', () => { loadStatus(); loadQuotes(); loadDetail(); });
  el.btnHoldings.addEventListener('click', () => {
    S.holdingsFull = false;
    loadHoldings();
  });
  el.btnHoldingsMode.addEventListener('click', () => {
    S.holdingsFull = !S.holdingsFull;
    loadHoldings();
  });
  el.holdingsBody.parentElement.parentElement.addEventListener('click', e => {
    const th = e.target.closest('th[data-hsort]');
    if (!th) return;
    const k = th.dataset.hsort;
    if (S.holdingsSort === k) S.holdingsDesc = !S.holdingsDesc;
    else { S.holdingsSort = k; S.holdingsDesc = k !== 'name' && k !== 'code'; }
    renderHoldings();
  });
  // 注意：持仓表头的点击已经在上面绑定，这里不要再绑一次
  document.querySelector('#wl thead').addEventListener('click', e => {
    const th = e.target.closest('th[data-sort]');
    if (!th) return;
    const k = th.dataset.sort;
    if (S.sortCol === k) S.sortDesc = !S.sortDesc; else { S.sortCol = k; S.sortDesc = false; }
    renderWatch();
  });

  // 添加 / 删除 / 排序
  const doAdd = async () => {
    const q = el.addInput.value.trim();
    if (!q) return;
    el.addInput.disabled = true;
    try {
      const d = await postJSON('/api/watchlist', { action: 'add', query: q });
      el.addInput.value = '';
      S.watch = d.list || S.watch;
      renderWatch();
      loadQuotes();
    } catch (e) {
      el.conn.textContent = '添加失败：' + e.message;
      el.conn.className = 'conn err';
    } finally { el.addInput.disabled = false; el.addInput.focus(); }
  };
  el.btnAdd.addEventListener('click', doAdd);
  el.addInput.addEventListener('keydown', e => { if (e.key === 'Enter') doAdd(); });

  const act = async body => {
    if (!S.selected) return;
    try {
      const d = await postJSON('/api/watchlist', body);
      S.watch = d.list || S.watch;
      renderWatch();
      return d;
    } catch (e) {
      el.conn.textContent = '操作失败：' + e.message;
      el.conn.className = 'conn err';
    }
  };
  el.btnDel.addEventListener('click', async () => {
    if (!S.selected) return;
    if (!confirm('从自选股删除？')) return;
    const removed = S.selected;
    const d = await act({ action: 'remove', secid: removed });
    if (d) { S.selected = S.watch.length ? S.watch[0].secid : null; renderWatch(); loadDetail(); }
  });
  el.btnUp.addEventListener('click', () => act({ action: 'move', secid: S.selected, delta: -1 }));
  el.btnDown.addEventListener('click', () => act({ action: 'move', secid: S.selected, delta: 1 }));

  // ---- 侧栏宽度：可拖拽 + 记忆 ----
  const sideEl = document.querySelector('.side');
  const splitter = document.querySelector('#splitter');
  const savedW = Number(localStorage.getItem('stockmon.sideWidth') || 0);
  if (savedW >= 300) sideEl.style.width = savedW + 'px';
  let spDrag = null;
  splitter.addEventListener('mousedown', e => {
    spDrag = { x: e.clientX, w: sideEl.getBoundingClientRect().width };
    splitter.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!spDrag) return;
    const w = Math.max(300, Math.min(640, spDrag.w + (e.clientX - spDrag.x)));
    sideEl.style.width = w + 'px';
  });
  window.addEventListener('mouseup', () => {
    if (!spDrag) return;
    spDrag = null;
    splitter.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    const w = Math.round(sideEl.getBoundingClientRect().width);
    try { localStorage.setItem('stockmon.sideWidth', String(w)); } catch (e) { /* 隐私模式 */ }
  });

  // ---- 窄宽度时自动隐藏自选表的次要列（永不横向裁切）----
  const wlWrap = document.querySelector('#wl-wrap');
  const applyNarrow = () => {
    const w = wlWrap.clientWidth;
    // 阈值按列宽 64+86+80+78+82 重算：保证任何一档下名称列都有 >= 110px
    wlWrap.classList.toggle('narrow-1', w < 540);   // 先藏「涨跌」（与涨跌幅重复）
    wlWrap.classList.toggle('narrow-2', w < 450);   // 再藏「代码」
    wlWrap.classList.toggle('narrow-3', w < 390);   // 最后藏「主力」
  };
  applyNarrow();
  try { new ResizeObserver(applyNarrow).observe(wlWrap); } catch (e) { /* 老浏览器 */ }

  // 十字光标
  el.chart.addEventListener('mousemove', chartMouse);
  el.chart.addEventListener('mouseleave', () => { hideTip(); S.hover = null; draw(); });

  // 分时缩放 / 平移 / 复位
  let drag = null;
  el.chart.addEventListener('wheel', e => {
    if (S.period !== 'tick' || !S.layout || S.layout.kind !== 'tick') {
      // K线：滚轮改可见根数
      if (S.kline) {
        e.preventDefault();
        S.viewBars = Math.max(20, Math.min(400, S.viewBars + (e.deltaY > 0 ? 16 : -16)));
        S.hover = null; hideTip(); draw();
      }
      return;
    }
    e.preventDefault();
    const lay = S.layout, rect = el.chart.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - lay.plotL) / lay.plotW));
    const [v0, v1] = S.tickView || [0, TICK_AXIS_MINUTES];
    const span = v1 - v0;
    const factor = e.deltaY < 0 ? 0.7 : 1 / 0.7;
    const nspan = Math.max(3, Math.min(TICK_AXIS_MINUTES, span * factor));
    let n0 = v0 + frac * span - frac * nspan;
    n0 = Math.max(0, Math.min(n0, TICK_AXIS_MINUTES - nspan));
    S.tickView = nspan >= TICK_AXIS_MINUTES - 0.5 ? null : [n0, n0 + nspan];
    S.hover = null; hideTip(); draw();
  }, { passive: false });

  el.chart.addEventListener('mousedown', e => {
    const lay = S.layout;
    if (!lay || lay.kind !== 'tick' || !S.tickView) return;
    drag = { x: e.clientX, v0: lay.v0, v1: lay.v1, w: lay.plotW };
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    const span = drag.v1 - drag.v0;
    const shift = -(e.clientX - drag.x) / drag.w * span;
    let n0 = Math.max(0, Math.min(drag.v0 + shift, TICK_AXIS_MINUTES - span));
    S.tickView = [n0, n0 + span];
    S.hover = null; hideTip(); draw();
  });
  window.addEventListener('mouseup', () => { drag = null; });
  el.chart.addEventListener('dblclick', () => {
    if (S.period === 'tick') { S.tickView = null; S.hover = null; hideTip(); draw(); }
  });

  // 资金流柱悬停
  let flowRaf = 0;
  el.flow.addEventListener('mousemove', e => {
    const lay = S.flowLayout;
    if (!lay) return;
    const rect = el.flow.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (x < lay.plotL || x > lay.plotR) { drawFlow(); return; }
    const idx = Math.max(0, Math.min(lay.list.length - 1, Math.floor((x - lay.plotL) / lay.bw)));
    const b = lay.list[idx];
    drawFlow();
    const g = el.flow.getContext('2d');
    const cx = lay.plotL + (idx + 0.5) * lay.bw;
    line(g, cx, lay.top, cx, lay.bottom, '#8b98a8', 1, [3, 3]);
    const lines = [
      `${b.date}  主力 ${fmtMoney(b.main_net)}`,
      `收盘 ${fmtPrice(b.close)}   涨跌 ${fmtPct(b.pct)}`,
      `超大 ${fmtMoney(b.super_net)}  大单 ${fmtMoney(b.big_net)}`,
      `中单 ${fmtMoney(b.mid_net)}  小单 ${fmtMoney(b.small_net)}`,
    ];
    g.save();
    g.font = `11px ${FONT}`;
    const bw2 = Math.max.apply(null, lines.map(l => g.measureText(l).width)) + 16;
    const bh = lines.length * 15 + 8;
    let bx = cx + 12;
    if (bx + bw2 > lay.plotR) bx = cx - 12 - bw2;
    bx = Math.max(2, bx);
    const by = Math.max(2, lay.top - bh - 2);
    g.fillStyle = '#1d2735'; g.strokeStyle = C.border;
    g.fillRect(bx, by, bw2, bh); g.strokeRect(bx + .5, by + .5, bw2 - 1, bh - 1);
    g.restore();
    lines.forEach((l, i) => text(g, bx + 8, by + 10 + i * 15, l, i ? C.dim : C.fg, 11, 'left'));
  });
  el.flow.addEventListener('mouseleave', () => { if (S.flowLayout) drawFlow(); });

  // 尺寸变化
  const ro = new ResizeObserver(() => {
    if (S.period === 'tick') drawTick(); else drawKline();
    drawFlow();
  });
  ro.observe(document.querySelector('.chart-box'));
  ro.observe(document.querySelector('.flow-box'));
}

// ---------------------------------------------------------------- 主循环
let timer = 0;
async function tick() {
  clearTimeout(timer);
  await loadQuotes();
  const interval = (S.trading ? S.refreshSec : S.idleSec) * 1000;
  timer = setTimeout(tick, Math.max(1000, interval));
}
let detailTimer = 0;
async function detailTick() {
  clearTimeout(detailTimer);
  await loadDetail();
  detailTimer = setTimeout(detailTick, S.trading ? 6000 : 30000);
}

async function boot() {
  bind();
  renderPeriods();
  await loadStatus();
  await loadWatchlist();
  if (S.selected) {
    await loadQuotes();
    await loadDetail();
  }
  loadCommo();
  setInterval(loadStatus, 30000);
  setInterval(loadCommo, 15000);
  tick();
  detailTick();
  for (const s of S.watch) {
    const q = S.quotes[s.secid];
    if (q && q.name) s.name = q.name;
  }
  renderWatch();
}

document.addEventListener('DOMContentLoaded', boot);
