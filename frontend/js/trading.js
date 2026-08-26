const API = window.location.origin;

let chart, candleSeries, volumeSeries;
let ws = null;
let currentProduct = 'TM2608';
let currentSide = 'buy';

function formatPrice(p) {
  if (p == null || Number.isNaN(Number(p))) return '—';
  return Number(p).toLocaleString('zh-TW', { maximumFractionDigits: 0 });
}

function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: el.clientHeight,
    layout: { background: { color: '#0d1117' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350',
    borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });

  volumeSeries = chart.addHistogramSeries({
    color: '#26a69a',
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
  });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  window.addEventListener('resize', () => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  });
}

async function loadKlines() {
  const res = await fetch(`${API}/api/klines?product=${currentProduct}&limit=500`);
  const data = await res.json();
  candleSeries.setData(data.map(k => ({
    time: k.time, open: k.open, high: k.high, low: k.low, close: k.close,
  })));
  volumeSeries.setData(data.map(k => ({
    time: k.time,
    value: k.volume,
    color: k.close >= k.open ? 'rgba(38,166,154,.4)' : 'rgba(239,83,80,.4)',
  })));
}

function connectWS() {
  if (ws) { ws.onclose = null; ws.close(); }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${window.location.host}/ws/${currentProduct}`);

  const dot = document.getElementById('ws-status');
  const countEl = document.getElementById('ws-count');
  let msgCount = 0;

  ws.onopen = () => { dot.classList.add('connected'); if (countEl) countEl.textContent = '0msg'; };
  ws.onclose = () => {
    dot.classList.remove('connected');
    setTimeout(connectWS, 3000);
  };
  ws.onerror = () => dot.classList.remove('connected');

  ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data);
    if (msg.type !== 'tick') return;
    msgCount++;
    if (countEl) countEl.textContent = `${msgCount}msg`;

    if (msg.price != null) {
      document.getElementById('ticker-price').textContent = formatPrice(msg.price);
    }
    if (msg.bid != null || msg.ask != null) {
      document.getElementById('ticker-bid-ask').textContent =
        `買 ${formatPrice(msg.bid)} / 賣 ${formatPrice(msg.ask)}`;
    }
    if (msg.kline) {
      try {
        candleSeries.update({ ...msg.kline });
        volumeSeries.update({
          time: msg.kline.time,
          value: msg.kline.volume || 0,
          color: msg.kline.close >= msg.kline.open ? 'rgba(38,166,154,.4)' : 'rgba(239,83,80,.4)',
        });
      } catch (_) { /* ignore duplicate time */ }
    }
    refreshAccount();
    refreshPositions();
    refreshOrders();
  };
}

async function refreshConnection() {
  const res = await fetch(`${API}/api/connection`);
  const d = await res.json();
  const badge = document.getElementById('conn-badge');
  if (d.connected) {
    badge.textContent = 'LIVE';
    badge.classList.remove('offline');
  } else {
    badge.textContent = d.error ? '離線' : '連線中…';
    badge.classList.add('offline');
    badge.title = d.error || '';
  }
}

async function refreshAccount() {
  try {
    const res = await fetch(`${API}/api/account`);
    const d = await res.json();
    document.getElementById('acc-user').textContent = d.user_id || '—';
    document.getElementById('acc-account').textContent = d.active_account || '—';
    document.getElementById('acc-env').textContent = d.environment === 2 ? '測試' : d.environment === 0 ? '正式' : String(d.environment ?? '—');
    const rights = (d.rights_raw || '').trim();
    document.getElementById('acc-rights').textContent = rights ? rights.slice(0, 80) + (rights.length > 80 ? '…' : '') : '—';
    if (d.last_price != null) {
      document.getElementById('ticker-price').textContent = formatPrice(d.last_price);
    }
  } catch (_) { /* ignore */ }
}

function renderTableRows(tbodyId, rows, emptyColspan = 5) {
  const tbody = document.getElementById(tbodyId);
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr><td colspan="${emptyColspan}" class="empty">無資料</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.product || '—'}</td>
      <td>${r.direction || '—'}</td>
      <td>${r.qty || '—'}</td>
      <td>${r.price || '—'}</td>
      <td style="font-size:10px;color:#8b949e">${r.raw || ''}</td>
    </tr>
  `).join('');
}

async function refreshPositions() {
  try {
    const res = await fetch(`${API}/api/positions`);
    const rows = await res.json();
    renderTableRows('positions-body', rows);
  } catch (_) { /* ignore */ }
}

async function refreshOrders() {
  try {
    const res = await fetch(`${API}/api/orders`);
    const rows = await res.json();
    renderTableRows('orders-body', rows);
  } catch (_) { /* ignore */ }
}

async function loadParams() {
  const res = await fetch(`${API}/api/live/params`);
  const schema = await res.json();
  const el = document.getElementById('params-panel');
  const strat = schema.strategy || {};
  const order = schema.order || {};
  el.innerHTML = [
    `策略: ${strat.strategy?.default || 'pullback'}`,
    `MA: ${strat.ma_fast?.default}/${strat.ma_mid?.default}/${strat.ma_slow?.default}`,
    `Pullback SL/TP: ${strat.pullback_stop_loss_points?.default}/${strat.pullback_take_profit_points?.default}`,
    `Breakout SL/TP: ${strat.breakout_stop_loss_points?.default}/${strat.breakout_take_profit_points?.default}`,
    `出場: ${strat.exit_mode?.default || 'with_sltp'}`,
    `委託: ${order.trade_type?.default === 2 ? 'FOK' : order.trade_type?.default}`,
  ].map(line => `<div>${line}</div>`).join('');
}

function bindSideTabs() {
  document.querySelectorAll('.side-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentSide = btn.dataset.side;
      const submit = document.getElementById('submit-btn');
      if (currentSide === 'buy') {
        submit.textContent = '買進 Long';
        submit.className = 'submit-btn long';
      } else {
        submit.textContent = '賣出 Short';
        submit.className = 'submit-btn short';
      }
    });
  });
}

async function placeOrder() {
  const msg = document.getElementById('order-msg');
  if (!confirm('確定送出委託？正式環境為真實下單。')) return;

  const payload = {
    product_code: currentProduct,
    side: currentSide,
    qty: Number(document.getElementById('order-qty').value || 1),
    price: document.getElementById('order-price').value.trim() || '0',
    trade_type: Number(document.getElementById('order-trade-type').value),
    new_close: Number(document.getElementById('order-new-close').value),
  };

  try {
    const res = await fetch(`${API}/api/order`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '下單失敗');
    const r = data.order_result || {};
    msg.className = 'order-msg ' + (r.success ? 'ok' : 'err');
    msg.textContent = `${r.message || ''} ${r.raw || ''}`.trim();
    refreshAccount();
    refreshPositions();
    refreshOrders();
  } catch (e) {
    msg.className = 'order-msg err';
    msg.textContent = e.message;
  }
}

async function onProductChange() {
  currentProduct = document.getElementById('product-select').value;
  await fetch(`${API}/api/product?product=${encodeURIComponent(currentProduct)}`, { method: 'POST' });
  await loadKlines();
  connectWS();
  refreshAccount();
  refreshPositions();
  refreshOrders();
}

function bindEvents() {
  bindSideTabs();
  document.getElementById('submit-btn').addEventListener('click', placeOrder);
  document.getElementById('product-select').addEventListener('change', () => {
    onProductChange().catch(console.error);
  });
}

async function init() {
  currentProduct = document.getElementById('product-select').value;
  initChart();
  bindEvents();
  await loadParams();
  await loadKlines();
  connectWS();
  await refreshConnection();
  await refreshAccount();
  await refreshPositions();
  await refreshOrders();

  setInterval(() => refreshConnection(), 5000);
  setInterval(() => refreshOrders(), 3000);
}

init().catch(console.error);