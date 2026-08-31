const API = window.location.origin;

let chart, candleSeries, volumeSeries, maFastSeries, maMidSeries, maSlowSeries;
let ws = null;
let currentProduct = 'TM2608';
let currentSide = 'buy';

function isVal(v) {
  return v != null && !Number.isNaN(Number(v)) && Number.isFinite(Number(v));
}

function formatPrice(p) {
  if (!isVal(p)) return '—';
  return Number(p).toLocaleString('zh-TW', { maximumFractionDigits: 0 });
}

function formatMoney(v) {
  if (!isVal(v)) return '—';
  return Number(v).toLocaleString('zh-TW', { maximumFractionDigits: 0 });
}

function setSignedMoney(elId, value) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = formatMoney(value);
  el.classList.remove('pos', 'neg');
  if (isVal(value)) {
    if (Number(value) > 0) el.classList.add('pos');
    else if (Number(value) < 0) el.classList.add('neg');
  }
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let klinesCache = [];
let signalRowsCache = [];

function updateChartLegend(kline, sig) {
  const el = document.getElementById('chart-legend');
  if (!el) return;

  let html = '';
  if (kline && isVal(kline.open) && isVal(kline.close)) {
    const c = Number(kline.close) >= Number(kline.open) ? '#ef4444' : '#22c55e';
    html += `<span style="color:${c}">O:${formatPrice(kline.open)} H:${formatPrice(kline.high)} L:${formatPrice(kline.low)} C:${formatPrice(kline.close)}</span>`;
  }

  if (sig) {
    if (isVal(sig.ma_fast)) html += `<span class="legend-item legend-ma5">MA5:${formatPrice(sig.ma_fast)}</span>`;
    if (isVal(sig.ma_mid)) html += `<span class="legend-item legend-ma20">MA20:${formatPrice(sig.ma_mid)}</span>`;
    if (isVal(sig.ma_slow)) html += `<span class="legend-item legend-ma60">MA60:${formatPrice(sig.ma_slow)}</span>`;
  }

  el.innerHTML = html;
}

function updateLiveMA(latestKline) {
  if (!latestKline || !signalRowsCache.length) return;
  const time = latestKline.time;
  const idx = signalRowsCache.findIndex((r) => r.time === time);
  if (idx !== -1) {
    signalRowsCache[idx].close = latestKline.close;
    signalRowsCache[idx].open = latestKline.open;
    signalRowsCache[idx].high = latestKline.high;
    signalRowsCache[idx].low = latestKline.low;
  } else {
    signalRowsCache.push({
      time: time,
      open: latestKline.open,
      high: latestKline.high,
      low: latestKline.low,
      close: latestKline.close,
      ma_fast: null,
      ma_mid: null,
      ma_slow: null,
    });
  }

  const closes = signalRowsCache.map((r) => r.close);
  const n = closes.length;
  const targetIdx = idx !== -1 ? idx : n - 1;

  const calcMA = (windowSize) => {
    if (targetIdx + 1 < windowSize) return null;
    let sum = 0;
    for (let i = targetIdx - windowSize + 1; i <= targetIdx; i++) {
      const v = closes[i];
      if (!isVal(v)) return null;
      sum += Number(v);
    }
    return sum / windowSize;
  };

  const ma5 = calcMA(5);
  const ma20 = calcMA(20);
  const ma60 = calcMA(60);

  signalRowsCache[targetIdx].ma_fast = ma5;
  signalRowsCache[targetIdx].ma_mid = ma20;
  signalRowsCache[targetIdx].ma_slow = ma60;

  if (isVal(ma5) && maFastSeries) maFastSeries.update({ time: time, value: Number(ma5) });
  if (isVal(ma20) && maMidSeries) maMidSeries.update({ time: time, value: Number(ma20) });
  if (isVal(ma60) && maSlowSeries) maSlowSeries.update({ time: time, value: Number(ma60) });

  updateChartLegend(latestKline, signalRowsCache[targetIdx]);
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
    timeScale: {
      borderColor: '#30363d',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 5,
      barSpacing: 8,
      minBarSpacing: 3,
    },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#ef4444', downColor: '#22c55e',
    borderVisible: false, wickUpColor: '#ef4444', wickDownColor: '#22c55e',
  });

  maFastSeries = chart.addLineSeries({ color: '#e6b800', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: 'MA5' });
  maMidSeries = chart.addLineSeries({ color: '#4a9eff', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: 'MA20' });
  maSlowSeries = chart.addLineSeries({ color: '#c792ea', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: 'MA60' });

  chart.subscribeCrosshairMove((param) => {
    let candleData = null;
    let ma5Val = null;
    let ma20Val = null;
    let ma60Val = null;

    if (param && param.time) {
      candleData = param.seriesData.get(candleSeries);
      const f = param.seriesData.get(maFastSeries);
      const m = param.seriesData.get(maMidSeries);
      const s = param.seriesData.get(maSlowSeries);
      if (f && isVal(f.value)) ma5Val = f.value;
      if (m && isVal(m.value)) ma20Val = m.value;
      if (s && isVal(s.value)) ma60Val = s.value;

      if (!isVal(ma5Val) || !isVal(ma20Val) || !isVal(ma60Val)) {
        const pTime = typeof param.time === 'object' ? param.time.timestamp || param.time : param.time;
        const match = signalRowsCache.find((r) => r.time == pTime);
        if (match) {
          if (!isVal(ma5Val) && isVal(match.ma_fast)) ma5Val = match.ma_fast;
          if (!isVal(ma20Val) && isVal(match.ma_mid)) ma20Val = match.ma_mid;
          if (!isVal(ma60Val) && isVal(match.ma_slow)) ma60Val = match.ma_slow;
        }
      }
    } else {
      if (klinesCache.length) candleData = klinesCache[klinesCache.length - 1];
      if (signalRowsCache.length) {
        const lastSig = signalRowsCache[signalRowsCache.length - 1];
        if (isVal(lastSig.ma_fast)) ma5Val = lastSig.ma_fast;
        if (isVal(lastSig.ma_mid)) ma20Val = lastSig.ma_mid;
        if (isVal(lastSig.ma_slow)) ma60Val = lastSig.ma_slow;
      }
    }

    updateChartLegend(candleData, { ma_fast: ma5Val, ma_mid: ma20Val, ma_slow: ma60Val });
  });

  // 2026-08-31 實測抓到的 bug：圖表是在頁面版面（.main-grid 的 flex/grid 排版）
  // 還沒完全定案前的那一刻建立的，createChart 當下量到的 el.clientWidth/Height
  // 可能不是最終尺寸；本來只靠 window resize 事件事後修正，但一般使用者打開
  // 頁面不會去拖動瀏覽器視窗，這個初始尺寸誤差永遠不會自己修正，canvas 內部
  // 繪圖緩衝區會卡在建立當下量到的錯誤尺寸，卻被 CSS 硬拉伸成容器該有的顯示
  // 尺寸——蠟燭、間距、MA 線看起來全部跟著扭曲，就是「K 線間隔很開/很亂/很怪」
  // 這類回報的根因，不是資料或時區問題。改用 ResizeObserver 監控容器「元素
  // 本身」的尺寸變化（不只是瀏覽器視窗），版面排版一定案就會立刻修正一次。
  //
  // 兩個實測踩到的坑，都跟 Chrome 官方 ResizeObserver 文件明講的注意事項一樣：
  // 1. callback 裡不能同步呼叫 chart.applyOptions()——applyOptions 可能讓
  //    canvas 尺寸變化，又回頭觸發同一個 observer，形成同步遞迴迴圈，瀏覽器
  //    渲染執行緒整個卡死（實測truly掛住超過 45 秒）。用 requestAnimationFrame
  //    把實際套用尺寸這件事延到下一個影格，跳出同步呼叫鏈。
  // 2. 不能同時保留舊的 window resize 監聽器——兩邊都在改同一個 chart 的
  //    尺寸，等於兩條路徑互相干擾。ResizeObserver 本身就會涵蓋「瀏覽器視窗
  //    改變導致容器尺寸改變」這個情境，window resize 監聽器整個是多餘的，拿掉。
  let resizeScheduled = false;
  const resizeObserver = new ResizeObserver(() => {
    if (resizeScheduled) return;
    resizeScheduled = true;
    requestAnimationFrame(() => {
      resizeScheduled = false;
      chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    });
  });
  resizeObserver.observe(el);

  // 保底修正：ResizeObserver 的初始通知（規格保證 observe() 之後一定會發生
  // 一次）實測發現在某些情況下（背景分頁、自動化工具控制的分頁）可能被瀏覽器
  // 的渲染管線延後、甚至完全不觸發（渲染管線本身被節流時，仰賴渲染步驟送出
  // 的通知也會一起被卡住）。setTimeout 走的是一般計時器佇列，不依賴渲染管線
  // 是否有在跑，一定會執行，用來在頁面剛載入、版面剛穩定的這個時間點做一次
  // 保底校正，不完全依賴 ResizeObserver 有沒有真的觸發。
  setTimeout(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }, 300);
}

let chartInitialFitted = false;

async function onProductChange() {
  const sel = document.getElementById('product-select');
  const code = (sel ? sel.value : '').trim().toUpperCase();
  if (!code) return;
  currentProduct = code;
  chartInitialFitted = false;
  try {
    await apiFetch(`${API}/api/product?product=${encodeURIComponent(code)}`, { method: 'POST' });
  } catch (_) { /* ignore */ }
  await loadKlines();
  await refreshConnection();
}

async function loadKlines() {
  const res = await apiFetch(`${API}/api/klines?product=${currentProduct}&limit=500`);
  const data = await res.json();
  // 2026-08-21 實測抓到的 bug：後端在非預期情況（例如還沒完全連線）可能回傳
  // 一個物件而不是陣列，klinesCache = data || [] 這時候會把整個物件指派進去，
  // 後面 .map() 直接炸掉。改成明確檢查是不是陣列，不是就當空資料處理。
  klinesCache = Array.isArray(data) ? data : [];
  candleSeries.setData(klinesCache.map(k => ({
    time: k.time, open: k.open, high: k.high, low: k.low, close: k.close,
  })));
  await loadSignals();

  if (!chartInitialFitted && klinesCache.length > 0 && chart) {
    const totalBars = klinesCache.length;
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, totalBars - 80),
      to: totalBars + 2,
    });
    chartInitialFitted = true;
  }
}

const STRATEGY_SHORT_CODE = {
  breakout_long: 'BL', breakout_short: 'BS',
  pullback_long: 'PL', pullback_short: 'PS',
};

async function loadSignals() {
  try {
    const [sigRes, tradeRes] = await Promise.all([
      apiFetch(`${API}/api/klines/signals?product=${currentProduct}&limit=500`),
      apiFetch(`${API}/api/strategy/trades`),
    ]);
    const rows = await sigRes.json();
    const trades = await tradeRes.json();
    signalRowsCache = (rows || []).map((r) => {
      const k = klinesCache.find((item) => item.time === r.time);
      return {
        time: r.time,
        open: isVal(r.open) ? r.open : (k ? k.open : null),
        high: isVal(r.high) ? r.high : (k ? k.high : null),
        low: isVal(r.low) ? r.low : (k ? k.low : null),
        close: isVal(r.close) ? r.close : (k ? k.close : null),
        ma_fast: r.ma_fast,
        ma_mid: r.ma_mid,
        ma_slow: r.ma_slow,
        breakout_signal: r.breakout_signal,
        pullback_signal: r.pullback_signal,
      };
    });

    maFastSeries.setData(signalRowsCache.filter(r => isVal(r.ma_fast)).map(r => ({ time: r.time, value: Number(r.ma_fast) })));
    maMidSeries.setData(signalRowsCache.filter(r => isVal(r.ma_mid)).map(r => ({ time: r.time, value: Number(r.ma_mid) })));
    maSlowSeries.setData(signalRowsCache.filter(r => isVal(r.ma_slow)).map(r => ({ time: r.time, value: Number(r.ma_slow) })));

    if (signalRowsCache.length) {
      const lastSig = signalRowsCache[signalRowsCache.length - 1];
      const lastK = klinesCache.length ? klinesCache[klinesCache.length - 1] : null;
      updateChartLegend(lastK, lastSig);
    }

    // 只畫「策略真正下單」的進出場點，不是原始 MA 交叉幾何訊號（同向訊號會被策略忽略，
    // 畫出來只會製造誤解：以為每次交叉都進場了）。
    const code = (sid) => STRATEGY_SHORT_CODE[sid] || sid;
    const markers = trades.map((t) => {
      const isLong = t.direction === 'long';
      if (t.type === 'entry') {
        return {
          time: t.time, position: isLong ? 'belowBar' : 'aboveBar',
          color: isLong ? '#26a69a' : '#ef5350',
          shape: isLong ? 'arrowUp' : 'arrowDown',
          text: `${code(t.strategy_id)} 進場`,
        };
      }
      const isWin = (t.pnl ?? 0) >= 0;
      return {
        time: t.time, position: isLong ? 'aboveBar' : 'belowBar',
        color: isWin ? '#26a69a' : '#ef5350',
        shape: isLong ? 'arrowDown' : 'arrowUp',
        text: `${code(t.strategy_id)} 出場${t.pnl != null ? (isWin ? ' +' : ' ') + Math.round(t.pnl) : ''}`,
      };
    });
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  } catch (e) {
    console.error(e);
  }
}

async function connectWS() {
  if (ws) { ws.onclose = null; ws.close(); }
  const token = await getAccessToken();
  if (!token) { window.location.href = 'login.html'; return; }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // 瀏覽器原生 WebSocket API 不能自訂 header，登入 token 只能用 query string 帶
  // （見 backend/api.py 的 /ws/{product_code} 驗證）。
  ws = new WebSocket(`${proto}//${window.location.host}/ws/${currentProduct}?token=${encodeURIComponent(token)}`);

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
        updateLiveMA(msg.kline);
      } catch (_) { /* ignore duplicate time */ }
    }
    refreshAccount();
    refreshPositions();
    refreshOrders();
  };
}

function applyTradingGuard(disabled) {
  const submit = document.getElementById('submit-btn');
  const stratBoxes = document.querySelectorAll('[id^="strategy-toggle-"]');
  const msg = document.getElementById('order-msg');
  if (disabled) {
    if (submit) { submit.disabled = true; submit.title = '下單已關閉，請先確認 K 線與報價'; }
    stratBoxes.forEach((box) => { box.disabled = true; box.title = '自動策略已關閉（允許真實下單未開啟）'; });
    if (msg && !msg.textContent) {
      msg.className = 'order-msg';
      msg.textContent = '下單已關閉，目前僅顯示報價與 K 線';
    }
  } else {
    if (submit) { submit.disabled = false; submit.title = ''; }
    stratBoxes.forEach((box) => { box.disabled = false; box.title = ''; });
  }
}

async function refreshConnection() {
  const res = await apiFetch(`${API}/api/connection`);
  const d = await res.json();
  applyTradingGuard(!!d.trading_disabled);
  const badge = document.getElementById('conn-badge');
  if (d.connected) {
    badge.textContent = d.trading_disabled ? 'LIVE·看盤' : 'LIVE';
    badge.classList.remove('offline');
  } else {
    badge.textContent = d.error ? '離線' : '連線中…';
    badge.classList.add('offline');
    badge.title = d.error || '';
  }
  // 商品碼對齊後端；並用 connection.quote 刷新表頭價（不依賴是否有新成交）
  // 使用者正在輸入框裡打字時不要覆蓋，避免每 5 秒的輪詢把還沒按確定的內容蓋掉。
  const q = d.quote || {};
  if (q.product_code) {
    const sel = document.getElementById('product-select');
    if (sel && document.activeElement !== sel && sel.value !== q.product_code) {
      sel.value = q.product_code;
      currentProduct = q.product_code;
    }
  }
  if (q.last_price != null) {
    document.getElementById('ticker-price').textContent = formatPrice(q.last_price);
  }
  if (q.bid != null || q.ask != null) {
    document.getElementById('ticker-bid-ask').textContent =
      `買 ${formatPrice(q.bid)} / 賣 ${formatPrice(q.ask)}`;
  }
}

async function refreshAccount() {
  try {
    const res = await apiFetch(`${API}/api/account`);
    const d = await res.json();
    document.getElementById('acc-user').textContent = d.user_id || '—';
    document.getElementById('acc-account').textContent = d.active_account || '—';
    document.getElementById('acc-env').textContent = d.environment === 2 ? '測試' : d.environment === 0 ? '正式' : String(d.environment ?? '—');
    setSignedMoney('acc-equity', d.equity);
    setSignedMoney('acc-floating-pl', d.floating_pl);
    setSignedMoney('acc-available', d.available_balance);
    document.getElementById('acc-maint-margin').textContent = formatMoney(d.maintenance_margin);
    document.getElementById('acc-risk').textContent = d.risk_indicator || '—';
    if (d.last_price != null) {
      document.getElementById('ticker-price').textContent = formatPrice(d.last_price);
    }
  } catch (_) { /* ignore */ }
}

async function refreshPositions() {
  try {
    const res = await apiFetch(`${API}/api/positions`);
    const rows = await res.json();
    const tbody = document.getElementById('positions-body');
    if (!rows || !rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">無持倉</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(r => {
      const dirKey = r.direction_key || '';
      const canClose = dirKey === 'long' || dirKey === 'short';
      const closeBtn = canClose
        ? `<button class="action-btn" type="button" onclick="closePosition('${escHtml(r.product)}','${escHtml(dirKey)}',${Number(r.qty) || 1})">平倉</button>`
        : '—';
      return `<tr>
        <td>${escHtml(r.product || '—')}</td>
        <td class="${dirKey === 'long' ? 'pos' : dirKey === 'short' ? 'neg' : ''}">${escHtml(r.direction || '—')}</td>
        <td>${escHtml(r.qty || '—')}</td>
        <td>${escHtml(r.price || '—')}</td>
        <td>${closeBtn}</td>
      </tr>`;
    }).join('');
  } catch (_) { /* ignore */ }
}

let currentOrderTab = 'active';

function switchOrderTab(tab) {
  currentOrderTab = tab;
  const activeEl = document.getElementById('tab-active-orders');
  const historyEl = document.getElementById('tab-history-orders');
  if (activeEl) activeEl.classList.toggle('active', tab === 'active');
  if (historyEl) historyEl.classList.toggle('active', tab === 'history');
  refreshOrders();
}

async function refreshOrders() {
  try {
    const res = await apiFetch(`${API}/api/orders`);
    const allRows = await res.json();
    const tbody = document.getElementById('orders-body');
    if (!allRows || !allRows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">無委託</td></tr>';
      return;
    }
    const rows = allRows.filter(r => currentOrderTab === 'active' ? r.is_active !== false : r.is_active === false);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">${currentOrderTab === 'active' ? '無有效委託' : '無歷史委託'}</td></tr>`;
      return;
    }
    // 時間欄位是 "YYYY-MM-DD HH:MM:SS" 字串格式，字串排序等同時間排序；最新的排最上面。
    rows.sort((a, b) => String(b.time || '').localeCompare(String(a.time || '')));
    tbody.innerHTML = rows.map(r => {
      const seq = r.seq_no || r.book_no || '';
      const cancelBtn = (r.is_active && seq)
        ? `<button class="action-btn danger" type="button" onclick="cancelOrder('${escHtml(seq)}')">撤單</button>`
        : '—';

      // 1. 性質 (新倉 / 平倉 / 當沖 / 自動)
      let flagText = '—';
      const flag = String(r.new_close_flag || '').toUpperCase();
      if (flag === 'N') flagText = '新倉';
      else if (flag === 'O') flagText = '平倉';
      else if (flag === 'Y') flagText = '當沖';
      else if (flag === 'A') flagText = '自動';
      else if (flag) flagText = flag;

      // 2. 買賣方向 (買進 / 賣出)
      let dirText = r.direction || '—';
      if (r.direction === '多' || r.direction_key === 'long') dirText = '買進';
      else if (r.direction === '空' || r.direction_key === 'short') dirText = '賣出';

      // 3. 口數 (已成交 / 委託口數)
      const filled = (r.filled_qty !== undefined && r.filled_qty !== '') ? r.filled_qty : (r.is_active ? '0' : (r.qty || '0'));
      const total = r.orig_qty || r.qty || '—';
      const qtyText = `${filled} / ${total}`;

      // 4. 序號/書號
      const orderId = r.book_no || r.seq_no || '—';

      return `<tr>
        <td>${escHtml(r.time || '—')}</td>
        <td>${escHtml(orderId)}</td>
        <td>${escHtml(r.product || '—')}</td>
        <td>${escHtml(flagText)}</td>
        <td>${escHtml(dirText)}</td>
        <td>${escHtml(qtyText)}</td>
        <td>${escHtml(r.price || '—')}</td>
        <td>${escHtml(r.status || '—')}</td>
        <td>${cancelBtn}</td>
      </tr>`;
    }).join('');
  } catch (_) { /* ignore */ }
}

let strategyDefs = [];
let myStrategyConfigs = {};

// 2026-08-26：帳號↔策略配對（見
// docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）——這個面板
// 現在只顯示「綁定給目前登入帳號」的策略，不是固定顯示全部 4 個。綁定關係
// 由後端直接寫入資料庫（見 backend/strategy_config_store.py 的
// admin_upsert_strategy_config），這裡沒有網頁表單可以自己新增綁定——上架
// 一個策略是工程操作（寫程式碼、部署、綁定資料庫），不是 Dashboard 上的
// 一般操作。啟動/停止那顆開關完全沒變，還是原本會真的下單的機制。
async function loadStrategyDefs() {
  try {
    const [listRes, mineRes] = await Promise.all([
      apiFetch(`${API}/api/strategy/list`),
      apiFetch(`${API}/api/my-strategy-configs`),
    ]);
    const allDefs = await listRes.json();
    const mineRows = await mineRes.json();
    myStrategyConfigs = Object.fromEntries(mineRows.map((r) => [r.strategy_id, r]));
    strategyDefs = allDefs.filter((d) => myStrategyConfigs[d.strategy_id]);
  } catch (e) {
    console.error(e);
    strategyDefs = [];
  }
  renderStrategyRows({});
}

function renderStrategyRows(statusMap) {
  const container = document.getElementById('strategy-list');
  const emptyEl = document.getElementById('strategy-list-empty');
  container.innerHTML = '';

  if (!strategyDefs.length) {
    if (emptyEl) emptyEl.classList.remove('hidden');
    return;
  }
  if (emptyEl) emptyEl.classList.add('hidden');

  strategyDefs.forEach((def) => {
    const sid = def.strategy_id;
    const st = statusMap[sid] || { armed: false };
    const cfg = myStrategyConfigs[sid] || {};
    const row = document.createElement('div');
    row.className = 'strategy-row';
    row.dataset.strategyId = sid;

    const head = document.createElement('div');
    head.className = 'strategy-row-head';

    const dot = document.createElement('span');
    dot.className = 'strategy-status-dot' + (st.armed ? ' armed' : st.stopped ? ' stopped' : '');
    dot.id = `strategy-dot-${sid}`;

    const label = document.createElement('label');
    label.textContent = def.label || sid;
    label.setAttribute('for', `strategy-toggle-${sid}`);

    const switchWrap = document.createElement('label');
    switchWrap.className = 'switch';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.id = `strategy-toggle-${sid}`;
    box.checked = !!st.armed;
    box.addEventListener('change', (ev) => onStrategyToggle(sid, ev.target.checked));
    const track = document.createElement('span');
    track.className = 'switch-track';
    switchWrap.appendChild(box);
    switchWrap.appendChild(track);

    head.appendChild(dot);
    head.appendChild(label);
    head.appendChild(switchWrap);

    // 專屬風控與商品規格摘要 (從 Supabase 取得)
    const meta = document.createElement('div');
    meta.className = 'strategy-meta-line';
    const prod = cfg.product_code || currentProduct;
    const qty = cfg.qty || 1;
    const sl = cfg.stop_loss_points != null ? `${cfg.stop_loss_points}點` : '預設';
    const tp = cfg.take_profit_points != null ? `${cfg.take_profit_points}點` : '預設';
    const maxLoss = cfg.max_loss_ntd != null ? `-${Number(cfg.max_loss_ntd).toLocaleString()}元` : '預設';
    meta.innerHTML = `<span>${prod} x${qty}口</span><span>停損:${sl} · 停利:${tp} · 上限:${maxLoss}</span>`;

    // 4 道防護網狀態徽章
    const badges = document.createElement('div');
    badges.className = 'strategy-badges';
    badges.id = `strategy-badges-${sid}`;

    const layers = [
      { key: 'oco_enabled', label: '1.OCO智慧單' },
      { key: 'soft_stop_enabled', label: '2.本地軟停損' },
      { key: 'risk_insurance_enabled', label: '3.風控保險' },
      { key: 'reverse_signal_exit_enabled', label: '4.反向平倉' },
    ];
    layers.forEach((l) => {
      const badge = document.createElement('span');
      const enabled = (st[l.key] !== undefined) ? (st[l.key] !== false) : (cfg[l.key] !== false);
      badge.className = `layer-badge ${enabled ? 'active' : 'inactive'}`;
      badge.textContent = `${enabled ? '✓' : '✗'} ${l.label}`;
      badge.title = `${l.label}: ${enabled ? '已開啟' : '已關閉'}`;
      badges.appendChild(badge);
    });

    const detail = document.createElement('div');
    detail.id = `strategy-detail-${sid}`;
    detail.className = 'strategy-row-detail' + (st.stopped ? ' stopped' : '');
    detail.textContent = strategyDetailText(st);

    row.appendChild(head);
    row.appendChild(meta);
    row.appendChild(badges);
    row.appendChild(detail);
    container.appendChild(row);
  });
}

function strategyDetailText(st) {
  if (st.armed) {
    let heartbeat = '尚未檢查';
    if (st.last_checked_at) {
      const secs = Math.max(0, Math.floor(Date.now() / 1000 - st.last_checked_at));
      heartbeat = `上次檢查 ${secs} 秒前`;
    }
    const pnl = Number(st.realized_pnl_ntd || 0);
    const held = st.held_qty ? `持倉 ${st.held_direction === 'long' ? '多' : '空'} x${st.held_qty} · ` : '空手 · ';
    const fails = Number(st.consecutive_failures || 0);
    const failNote = fails > 0 ? ` · ⚠ 連續失敗 ${fails} 次` : '';
    return `運行中 · ${held}損益 ${pnl >= 0 ? '+' : ''}${pnl.toLocaleString('zh-TW')} 元 · ${heartbeat}${failNote}` +
      (st.last_action ? ` · ${st.last_action}` : '');
  }
  if (st.stopped) {
    return `已停止：${st.stop_reason || '風控或手動停止'}`;
  }
  return '未啟動';
}

async function refreshStrategyStatus() {
  try {
    const res = await apiFetch(`${API}/api/strategy/status`);
    const statusMap = await res.json();
    if (!strategyDefs.length) {
      await loadStrategyDefs();
    }
    strategyDefs.forEach((def) => {
      const sid = def.strategy_id;
      const box = document.getElementById(`strategy-toggle-${sid}`);
      const detail = document.getElementById(`strategy-detail-${sid}`);
      const dot = document.getElementById(`strategy-dot-${sid}`);
      const badges = document.getElementById(`strategy-badges-${sid}`);
      const cfg = myStrategyConfigs[sid] || {};
      if (!box || !detail) return;
      const st = statusMap[sid] || { armed: false };
      box.checked = !!st.armed;
      detail.className = 'strategy-row-detail' + (st.stopped ? ' stopped' : '');
      detail.textContent = strategyDetailText(st);
      if (dot) dot.className = 'strategy-status-dot' + (st.armed ? ' armed' : st.stopped ? ' stopped' : '');

      if (badges) {
        const layers = [
          { key: 'oco_enabled', label: '1.OCO智慧單' },
          { key: 'soft_stop_enabled', label: '2.本地軟停損' },
          { key: 'risk_insurance_enabled', label: '3.風控保險' },
          { key: 'reverse_signal_exit_enabled', label: '4.反向平倉' },
        ];
        badges.innerHTML = '';
        layers.forEach((l) => {
          const badge = document.createElement('span');
          const enabled = (st[l.key] !== undefined) ? (st[l.key] !== false) : (cfg[l.key] !== false);
          badge.className = `layer-badge ${enabled ? 'active' : 'inactive'}`;
          badge.textContent = `${enabled ? '✓' : '✗'} ${l.label}`;
          badge.title = `${l.label}: ${enabled ? '已開啟' : '已關閉'}`;
          badges.appendChild(badge);
        });
      }
    });
  } catch (_) { /* ignore */ }
}

async function onStrategyToggle(strategyId, checked) {
  const detail = document.getElementById(`strategy-detail-${strategyId}`);
  try {
    if (!checked) {
      await apiFetch(`${API}/api/strategy/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_id: strategyId }),
      });
    } else {
      const cfg = myStrategyConfigs[strategyId] || {};
      const payload = {
        strategy_id: strategyId,
        product_code: cfg.product_code || currentProduct,
        qty: cfg.qty != null ? Number(cfg.qty) : 1,
        stop_loss_points: cfg.stop_loss_points != null ? Number(cfg.stop_loss_points) : undefined,
        take_profit_points: cfg.take_profit_points != null ? Number(cfg.take_profit_points) : undefined,
        max_loss_ntd: cfg.max_loss_ntd != null ? Number(cfg.max_loss_ntd) : undefined,
        max_loss_pct: cfg.max_loss_pct != null ? Number(cfg.max_loss_pct) : undefined,
        oco_enabled: cfg.oco_enabled !== false,
        soft_stop_enabled: cfg.soft_stop_enabled !== false,
        risk_insurance_enabled: cfg.risk_insurance_enabled !== false,
        reverse_signal_exit_enabled: cfg.reverse_signal_exit_enabled !== false,
      };
      const res = await apiFetch(`${API}/api/strategy/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '啟動失敗');
    }
    refreshStrategyStatus();
  } catch (e) {
    if (detail) detail.textContent = e.message;
    const box = document.getElementById(`strategy-toggle-${strategyId}`);
    if (box) box.checked = !checked;
  }
}

function warnReconciledStrategies(data) {
  const affected = data && data.reconciled_strategies;
  if (!affected || !affected.length) return;
  alert(
    `⚠ 偵測到以下策略的內部持倉記錄跟券商實際部位對不上，已自動停止並清空記錄，` +
    `請務必人工核對實際帳戶損益：\n${affected.join('、')}`
  );
  refreshStrategyStatus();
}

async function flattenAll() {
  if (!confirm('確定一鍵平倉所有持倉？')) return;
  try {
    const res = await apiFetch(`${API}/api/positions/flatten`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '平倉失敗');
    alert(`已送出 ${data.closed || 0} 筆平倉委託`);
    refreshPositions();
    refreshOrders();
    warnReconciledStrategies(data);
  } catch (e) {
    alert(e.message);
  }
}

async function closePosition(product, directionKey, qty) {
  if (!confirm(`確定平倉 ${product} ${directionKey === 'long' ? '多' : '空'} ${qty} 口？`)) return;
  try {
    const res = await apiFetch(`${API}/api/positions/close`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_code: product, direction_key: directionKey, qty }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '平倉失敗');
    refreshPositions();
    refreshOrders();
    warnReconciledStrategies(data);
  } catch (e) {
    alert(e.message);
  }
}

async function cancelOrder(seqNo) {
  if (!confirm(`確定撤銷委託書號 ${seqNo}？`)) return;
  try {
    const res = await apiFetch(`${API}/api/order/${encodeURIComponent(seqNo)}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '撤單失敗');
    refreshOrders();
  } catch (e) {
    alert(e.message);
  }
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
    const res = await apiFetch(`${API}/api/order`, {
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
  const input = document.getElementById('product-select');
  const code = input.value.trim().toUpperCase();
  if (!code) {
    alert('請輸入商品代碼');
    return;
  }
  input.value = code;
  currentProduct = code;
  await apiFetch(`${API}/api/product?product=${encodeURIComponent(currentProduct)}`, { method: 'POST' });
  await loadKlines();
  connectWS();
  refreshAccount();
  refreshPositions();
  refreshOrders();
  refreshStrategyStatus();
}

function renderTradingSafety(disabled) {
  const box = document.getElementById('trading-safety-toggle');
  const msg = document.getElementById('trading-safety-msg');
  box.checked = !disabled;
  if (disabled) {
    msg.textContent = '目前關閉真實下單（安全模式），下單/自動策略只會顯示錯誤不會真的送單';
    msg.style.color = '#8b949e';
  } else {
    msg.textContent = '⚠ 已開啟真實下單，送出的委託會真的進到你的群益帳戶';
    msg.style.color = '#e5534b';
  }
}

async function refreshTradingSafety() {
  try {
    const res = await apiFetch(`${API}/api/trading/safety`);
    const data = await res.json();
    renderTradingSafety(data.trading_disabled);
  } catch (e) {
    console.error(e);
  }
}

async function onTradingSafetyToggle(ev) {
  const wantEnable = ev.target.checked;
  let confirmValue = '';
  if (wantEnable) {
    const warned = confirm(
      '確定要開啟真實下單嗎？\n' +
      '開啟後，這個系統送出的每一筆委託都是真實的群益期貨帳戶下單，會有真實資金曝險。\n' +
      '請先確認報價、K 線、策略參數都正常，再繼續。'
    );
    if (!warned) {
      ev.target.checked = false;
      return;
    }
    confirmValue = prompt('請輸入 ENABLE 以確認開啟真實下單：') || '';
    if (confirmValue !== 'ENABLE') {
      alert('輸入不符，已取消，真實下單維持關閉。');
      ev.target.checked = false;
      return;
    }
  }

  try {
    const res = await apiFetch(`${API}/api/trading/safety`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled: !wantEnable, confirm: confirmValue }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '設定失敗');
    renderTradingSafety(data.trading_disabled);
  } catch (e) {
    alert(e.message);
    refreshTradingSafety();
  }
}

function bindEvents() {
  bindSideTabs();
  document.getElementById('submit-btn').addEventListener('click', placeOrder);
  document.getElementById('trading-safety-toggle').addEventListener('change', onTradingSafetyToggle);
  document.getElementById('product-confirm-btn').addEventListener('click', () => {
    onProductChange().catch(console.error);
  });
  document.getElementById('product-select').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') onProductChange().catch(console.error);
  });
}

async function init() {
  const token = await requireAuth();
  if (!token) return; // requireAuth 已經導去登入頁

  currentProduct = document.getElementById('product-select').value;
  initChart();
  bindEvents();
  // 2026-08-21 實測抓到的 bug：HTML 裡商品代碼輸入框的預設值是寫死的字串
  // （合約到期後就過期），loadKlines() 原本在 refreshConnection() 之前執行，
  // 代表第一次抓K線一定是用這個可能已經過期的預設值，不是後端實際連線中的
  // 近月合約——結果就是空的K線陣列，畫面上完全看不到K棒，要等下一輪
  // setInterval(refreshConnection) 才會校正回正確商品，使用者體感像是「圖是空的」。
  // 改成先跟後端要到真正連線中的商品碼，再用它去抓K線。
  await refreshConnection();
  await loadKlines();
  connectWS();
  await refreshAccount();
  await refreshPositions();
  await refreshOrders();
  await loadStrategyDefs();
  await refreshStrategyStatus();
  await refreshTradingSafety();

  setInterval(() => refreshConnection(), 5000);
  setInterval(() => refreshOrders(), 3000);
  setInterval(() => refreshStrategyStatus(), 5000);
  setInterval(() => refreshTradingSafety(), 5000);
  // 2026-08-31 實測抓到的 bug：蠟燭圖本體（candleSeries）只有頁面第一次載入時
  // 呼叫過 loadKlines() 一次，之後完全不會再更新——只有 MA 線/訊號疊圖靠
  // loadSignals() 每 60 秒刷新，導致蠟燭圖凍結在頁面剛打開那一刻，跟後端
  // 實際的 K 線資料越差越多，畫面看起來會逐漸「跟現在對不上」。loadKlines()
  // 本身最後就會呼叫 loadSignals()，這裡改成定時呼叫 loadKlines()，兩邊一起
  // 刷新，不需要也不該再分開各跑各的。setData() 不會重置使用者當下的縮放/
  // 平移位置（這個檔案沒有呼叫 fitContent()），刷新不會打斷使用者正在看的範圍。
  setInterval(() => loadKlines(), 60000);
}

init().catch(console.error);