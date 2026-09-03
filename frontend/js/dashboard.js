'use strict';

const API_BASE = '';
const FALLBACK_API_BASE = 'http://127.0.0.1:8000';
const DEFAULT_SUMMARY = {
  total_trades: null,
  win_rate: null,
  total_net_pnl_ntd: null,
  max_drawdown_ntd: null,
  profit_factor: null,
  avg_win_ntd: null,
  avg_loss_ntd: null,
};

const state = {
  lastApiBase: API_BASE,
  lastResult: null,
  candleChart: null,
  candleSeries: null,
  maFastSeries: null,
  maMidSeries: null,
  maSlowSeries: null,
  equityChart: null,
  equitySeries: null,
};

const elements = {
  strategyViewSelect: document.getElementById('strategy-view-select'),
  runButton: document.getElementById('run-button'),
  datasetMeta: document.getElementById('dataset-meta'),
  connectionStatus: document.getElementById('connection-status'),
  errorBanner: document.getElementById('error-banner'),
  loadingOverlay: document.getElementById('loading-overlay'),
  chartCaption: document.getElementById('chart-caption'),
  tradeLogCaption: document.getElementById('trade-log-caption'),
  tradeTableBody: document.getElementById('trade-table-body'),
  summaryTotalTrades: document.getElementById('summary-total-trades'),
  summaryWinRate: document.getElementById('summary-win-rate'),
  summaryTotalNetPnl: document.getElementById('summary-total-net-pnl'),
  summaryMaxDrawdown: document.getElementById('summary-max-drawdown'),
  summaryProfitFactor: document.getElementById('summary-profit-factor'),
  summaryAvgWinLoss: document.getElementById('summary-avg-win-loss'),
  candleChart: document.getElementById('candle-chart'),
  equityChart: document.getElementById('equity-chart'),
  live4StartDate: document.getElementById('live4-start-date'),
  live4EndDate: document.getElementById('live4-end-date'),
  live4StopLossPoints: document.getElementById('live4-stop-loss-points'),
  live4TakeProfitPoints: document.getElementById('live4-take-profit-points'),
  live4ReverseExitCheckbox: document.getElementById('live4-reverse-exit-checkbox'),
  live4StopTakeCheckbox: document.getElementById('live4-stop-take-checkbox'),
  live4CapitalInput: document.getElementById('live4-capital-input'),
  live4MaxLossNtd: document.getElementById('live4-max-loss-ntd'),
  live4MaxLossPct: document.getElementById('live4-max-loss-pct'),
  live4RiskStopCheckbox: document.getElementById('live4-risk-stop-checkbox'),
  live4SummaryPanel: document.getElementById('live4-summary-panel'),
  live4StrategyCards: document.getElementById('live4-strategy-cards'),
  summaryGrid: document.getElementById('summary-grid'),
};

const STRATEGY_SHORT_CODE = {
  breakout_long: 'BL', breakout_short: 'BS',
  pullback_long: 'PL', pullback_short: 'PS',
};

function getCandidateApiBases() {
  const bases = [];
  if (API_BASE) {
    bases.push(API_BASE);
  } else {
    bases.push('');
  }
  if (FALLBACK_API_BASE && !bases.includes(FALLBACK_API_BASE)) {
    bases.push(FALLBACK_API_BASE);
  }
  return bases;
}

async function apiRequest(path, options = {}) {
  const bases = getCandidateApiBases();
  let lastError = null;

  // 2026-08-21：登入改用 Supabase，所有打自家 API 的請求都要帶 token，
  // 這裡是唯一的呼叫入口（app.js 沒有這種集中封裝，才需要逐一改成 apiFetch）。
  let token = await getAccessToken();
  if (!token) {
    window.location.href = 'login.html';
    throw new Error('尚未登入');
  }

  for (let index = 0; index < bases.length; index += 1) {
    const base = bases[index];
    const isLastAttempt = index === bases.length - 1;
    const url = `${base}${path}`;
    const authOptions = Object.assign({}, options, {
      headers: Object.assign({}, options.headers, { Authorization: `Bearer ${token}` }),
    });

    try {
      let response = await fetch(url, authOptions);
      if (response.status === 401) {
        // 2026-08-24 實測抓到的競態：supabase-js 連續呼叫 getSession() 有時候
        // 會回傳 null（已知的 supabase-js 時序問題，見 auth.js 的說明），先
        // 強制重新問一次 session 重試一次，還是失敗才真的當作沒登入。
        token = await getAccessToken(true);
        if (token) {
          response = await fetch(url, Object.assign({}, options, {
            headers: Object.assign({}, options.headers, { Authorization: `Bearer ${token}` }),
          }));
        }
        if (!token || response.status === 401) {
          window.location.href = 'login.html';
          // 2026-08-21：同 app.js 的 apiFetch，不能直接 return 401 回應讓呼叫端
          // 拿去當正常資料解析，要 throw 讓它乾淨停下來。
          throw new Error('登入已過期，請重新登入');
        }
      }
      if (response.ok) {
        state.lastApiBase = base;
        return response;
      }

      const shouldTryFallback = !base && !isLastAttempt && response.status === 404;
      if (shouldTryFallback) {
        continue;
      }
      return response;
    } catch (error) {
      lastError = error;
      if (isLastAttempt) {
        throw error;
      }
    }
  }

  throw lastError || new Error('無法連線至後端 API。');
}

async function parseJsonResponse(response) {
  const payload = await response.json().catch(() => null);
  if (response.ok) {
    return payload;
  }

  const detail = payload && typeof payload === 'object'
    ? payload.detail || payload.message || JSON.stringify(payload)
    : response.statusText;
  throw new Error(detail || `HTTP ${response.status}`);
}

function initializeCharts() {
  state.candleChart = LightweightCharts.createChart(elements.candleChart, {
    layout: {
      background: { color: '#0b1220' },
      textColor: '#cbd5e1',
    },
    grid: {
      vertLines: { color: '#1f2937' },
      horzLines: { color: '#1f2937' },
    },
    rightPriceScale: {
      borderColor: '#334155',
    },
    timeScale: {
      borderColor: '#334155',
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    width: elements.candleChart.clientWidth,
    height: elements.candleChart.clientHeight,
  });

  state.candleSeries = state.candleChart.addCandlestickSeries({
    upColor: '#ef4444',
    downColor: '#22c55e',
    borderVisible: false,
    wickUpColor: '#ef4444',
    wickDownColor: '#22c55e',
  });

  state.maFastSeries = state.candleChart.addLineSeries({
    color: '#f59e0b',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  state.maMidSeries = state.candleChart.addLineSeries({
    color: '#38bdf8',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  state.maSlowSeries = state.candleChart.addLineSeries({
    color: '#a78bfa',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  state.equityChart = LightweightCharts.createChart(elements.equityChart, {
    layout: {
      background: { color: '#0b1220' },
      textColor: '#cbd5e1',
    },
    grid: {
      vertLines: { color: '#1f2937' },
      horzLines: { color: '#1f2937' },
    },
    rightPriceScale: {
      borderColor: '#334155',
    },
    timeScale: {
      borderColor: '#334155',
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Magnet,
    },
    width: elements.equityChart.clientWidth,
    height: elements.equityChart.clientHeight,
  });

  state.equitySeries = state.equityChart.addLineSeries({
    color: '#38bdf8',
    lineWidth: 2,
  });

  const resizeCharts = () => {
    state.candleChart.applyOptions({
      width: elements.candleChart.clientWidth,
      height: elements.candleChart.clientHeight,
    });
    state.equityChart.applyOptions({
      width: elements.equityChart.clientWidth,
      height: elements.equityChart.clientHeight,
    });
  };

  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(resizeCharts);
    observer.observe(elements.candleChart);
    observer.observe(elements.equityChart);
  } else {
    window.addEventListener('resize', resizeCharts);
  }
}

function setLoading(isLoading, message) {
  elements.runButton.disabled = isLoading;
  elements.loadingOverlay.classList.toggle('hidden', !isLoading);
  if (isLoading) {
    elements.connectionStatus.className = 'status-pill loading';
  }
  if (message) {
    elements.connectionStatus.textContent = message;
  }
}

function showError(message) {
  elements.errorBanner.textContent = message;
  elements.errorBanner.classList.remove('hidden');
  elements.connectionStatus.textContent = '請檢查 API 狀態';
  elements.connectionStatus.className = 'status-pill error';
}

function clearError() {
  elements.errorBanner.textContent = '';
  elements.errorBanner.classList.add('hidden');
}

function setIdleStatus(message, kind = 'success') {
  elements.connectionStatus.textContent = message;
  elements.connectionStatus.className = `status-pill${kind ? ` ${kind}` : ''}`;
}

function formatPercent(value) {
  if (value == null || Number.isNaN(value)) {
    return '—';
  }
  return `${(value * 100).toFixed(2)}%`;
}

function formatNumber(value, digits = 2) {
  if (value == null || Number.isNaN(value)) {
    return '—';
  }
  return Number(value).toLocaleString('zh-TW', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatInteger(value) {
  if (value == null || Number.isNaN(value)) {
    return '—';
  }
  return Number(value).toLocaleString('zh-TW');
}

function formatCurrency(value) {
  if (value == null || Number.isNaN(value)) {
    return '—';
  }
  return `NT$ ${Number(value).toLocaleString('zh-TW', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}`;
}

function formatDateTime(unixSeconds) {
  if (!unixSeconds && unixSeconds !== 0) {
    return '—';
  }
  const date = new Date(unixSeconds * 1000);
  return date.toLocaleString('zh-TW', {
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function updateSummary(summary = DEFAULT_SUMMARY) {
  elements.summaryTotalTrades.textContent = formatInteger(summary.total_trades);
  elements.summaryWinRate.textContent = formatPercent(summary.win_rate);
  elements.summaryTotalNetPnl.textContent = formatCurrency(summary.total_net_pnl_ntd);
  elements.summaryMaxDrawdown.textContent = formatCurrency(summary.max_drawdown_ntd);
  elements.summaryProfitFactor.textContent = summary.profit_factor == null
    ? '—'
    : Number(summary.profit_factor).toFixed(2);
  elements.summaryAvgWinLoss.textContent = `${formatCurrency(summary.avg_win_ntd)} / ${formatCurrency(summary.avg_loss_ntd)}`;

  applySignedClass(elements.summaryTotalNetPnl, summary.total_net_pnl_ntd);
  applySignedClass(elements.summaryMaxDrawdown, summary.max_drawdown_ntd != null ? -Math.abs(summary.max_drawdown_ntd) : null);
}

function applySignedClass(element, value) {
  element.classList.remove('positive', 'negative');
  if (value == null || Number.isNaN(value)) {
    return;
  }
  if (value > 0) {
    element.classList.add('positive');
  } else if (value < 0) {
    element.classList.add('negative');
  }
}

function renderTrades(trades) {
  elements.tradeLogCaption.textContent = `共 ${trades.length} 筆交易`;

  if (!trades.length) {
    elements.tradeTableBody.innerHTML = '<tr><td colspan="13" class="empty-row">本次回測沒有成交紀錄</td></tr>';
    return;
  }

  elements.tradeTableBody.innerHTML = trades.map((trade) => {
    const pnlClass = trade.net_pnl_ntd > 0 ? 'positive' : trade.net_pnl_ntd < 0 ? 'negative' : '';
    const directionText = trade.direction === 'long' ? '多單' : '空單';
    const directionClass = trade.direction === 'long' ? 'direction-long' : 'direction-short';
    const reasonClass = trade.exit_reason === 'take_profit'
      ? 'reason-profit'
      : (trade.exit_reason === 'stop_loss' || trade.exit_reason === 'risk_stop')
        ? 'reason-loss'
        : 'reason-neutral';

    return `
      <tr>
        <td>${trade.strategy_label || '—'}</td>
        <td>${formatDateTime(trade.entry_time)}</td>
        <td><span class="direction-pill ${directionClass}">${directionText}</span></td>
        <td>${formatNumber(trade.entry_price)}</td>
        <td>${formatDateTime(trade.exit_time)}</td>
        <td>${formatNumber(trade.exit_price)}</td>
        <td><span class="reason-pill ${reasonClass}">${trade.exit_reason}</span></td>
        <td>${formatNumber(trade.stop_loss_price)}</td>
        <td>${formatNumber(trade.take_profit_price)}</td>
        <td>${formatNumber(trade.gross_pnl_points)}</td>
        <td>${formatCurrency(trade.gross_pnl_ntd)}</td>
        <td>${formatCurrency(trade.total_cost_ntd)}</td>
        <td class="${pnlClass}">${formatCurrency(trade.net_pnl_ntd)}</td>
      </tr>
    `;
  }).join('');
}

function lineDataFromKlines(klines, key) {
  return klines
    .filter((item) => item[key] != null)
    .map((item) => ({ time: item.time, value: item[key] }));
}

function renderLive4StrategyCards(strategies) {
  const order = ['breakout_long', 'breakout_short', 'pullback_long', 'pullback_short'];
  elements.live4StrategyCards.innerHTML = order.map((strategyId) => {
    const entry = strategies[strategyId];
    if (!entry) return '';
    const summary = entry.summary || {};
    const pnlClass = (summary.total_net_pnl_ntd || 0) > 0 ? 'positive' : (summary.total_net_pnl_ntd || 0) < 0 ? 'negative' : '';
    return `
      <article class="strategy-card">
        <span class="stat-label">${entry.label}（${STRATEGY_SHORT_CODE[strategyId]}）</span>
        <strong>${formatInteger(summary.total_trades)} 筆　勝率 ${formatPercent(summary.win_rate)}</strong>
        <span class="${pnlClass}">淨損益 ${formatCurrency(summary.total_net_pnl_ntd)}</span>
        <span class="stat-label">最大回撤 ${formatCurrency(summary.max_drawdown_ntd)}　PF ${summary.profit_factor == null ? '—' : Number(summary.profit_factor).toFixed(2)}</span>
      </article>
    `;
  }).join('');
}

function markersFromStrategies(strategies) {
  const markers = [];
  for (const [strategyId, entry] of Object.entries(strategies)) {
    const code = STRATEGY_SHORT_CODE[strategyId] || strategyId;
    for (const trade of entry.trades || []) {
      const isLong = trade.direction === 'long';
      markers.push({
        time: trade.entry_time,
        position: isLong ? 'belowBar' : 'aboveBar',
        color: isLong ? '#ef4444' : '#22c55e',
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: `${code} 進場`,
      });
      const isWin = trade.net_pnl_ntd >= 0;
      markers.push({
        time: trade.exit_time,
        position: isLong ? 'aboveBar' : 'belowBar',
        color: isWin ? '#22c55e' : '#ef4444',
        shape: isLong ? 'arrowDown' : 'arrowUp',
        text: `${code} 出場${trade.net_pnl_ntd != null ? (isWin ? ' +' : ' ') + Math.round(trade.net_pnl_ntd) : ''}`,
      });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  return markers;
}

// 單一策略檢視 vs 四策略檢視，兩者用同一份 API 回應（4 個策略的完整資料都在裡面），
// 差別只在畫面上要不要濾成只顯示其中一個——不是兩套不同的回測邏輯或兩次不同的 API 呼叫。
function selectedView(result) {
  const viewId = elements.strategyViewSelect.value;
  const strategies = result.strategies || {};

  if (viewId === 'all') {
    let trades = [];
    for (const [strategyId, entry] of Object.entries(strategies)) {
      const label = `${entry.label}(${STRATEGY_SHORT_CODE[strategyId] || strategyId})`;
      trades = trades.concat((entry.trades || []).map((trade) => ({ ...trade, strategy_label: label })));
    }
    trades.sort((a, b) => a.entry_time - b.entry_time);
    return { trades, markerStrategies: strategies, isAll: true };
  }

  const entry = strategies[viewId];
  if (!entry) {
    return { trades: [], markerStrategies: {}, isAll: false };
  }
  const label = `${entry.label}(${STRATEGY_SHORT_CODE[viewId] || viewId})`;
  const trades = (entry.trades || []).map((trade) => ({ ...trade, strategy_label: label }));
  return { trades, markerStrategies: { [viewId]: entry }, isAll: false };
}

function summarizeTrades(trades) {
  const netPnls = trades.map((t) => Number(t.net_pnl_ntd) || 0);
  const wins = netPnls.filter((v) => v > 0);
  const losses = netPnls.filter((v) => v < 0);

  let cumulative = 0;
  let peak = 0;
  let maxDrawdown = 0;
  for (const pnl of netPnls) {
    cumulative += pnl;
    peak = Math.max(peak, cumulative);
    maxDrawdown = Math.max(maxDrawdown, peak - cumulative);
  }

  const sumWins = wins.reduce((a, b) => a + b, 0);
  const sumLosses = losses.reduce((a, b) => a + b, 0);
  let profitFactor;
  if (losses.length) {
    profitFactor = wins.length ? sumWins / Math.abs(sumLosses) : 0;
  } else {
    profitFactor = wins.length ? Infinity : 0;
  }

  return {
    total_trades: trades.length,
    win_rate: trades.length ? wins.length / trades.length : 0,
    total_net_pnl_ntd: netPnls.reduce((a, b) => a + b, 0),
    max_drawdown_ntd: maxDrawdown,
    profit_factor: profitFactor,
    avg_win_ntd: wins.length ? sumWins / wins.length : 0,
    avg_loss_ntd: losses.length ? sumLosses / losses.length : 0,
  };
}

function buildEquityCurve(klines, trades) {
  const sorted = [...trades].sort((a, b) => a.exit_time - b.exit_time);
  const startTime = klines.length ? klines[0].time : (sorted[0] ? sorted[0].entry_time : 0);
  const curve = [{ time: startTime, equity: 0 }];
  let cumulative = 0;
  for (const trade of sorted) {
    cumulative += Number(trade.net_pnl_ntd) || 0;
    curve.push({ time: trade.exit_time, equity: cumulative });
  }
  return curve;
}

function renderResult(result) {
  const candleData = result.klines.map((item) => ({
    time: item.time, open: item.open, high: item.high, low: item.low, close: item.close,
  }));
  state.candleSeries.setData(candleData);
  state.maFastSeries.setData(lineDataFromKlines(result.klines, 'ma_fast'));
  state.maMidSeries.setData(lineDataFromKlines(result.klines, 'ma_mid'));
  state.maSlowSeries.setData(lineDataFromKlines(result.klines, 'ma_slow'));

  const { trades, markerStrategies, isAll } = selectedView(result);
  state.candleSeries.setMarkers(markersFromStrategies(markerStrategies));
  state.candleChart.timeScale().fitContent();
  elements.chartCaption.textContent =
    `K 線 ${formatInteger(result.klines.length)} 根｜資料區間 ${(result.data_span || []).join(' ~ ')}`;

  const summary = summarizeTrades(trades);
  updateSummary(summary);

  state.equitySeries.setData(buildEquityCurve(result.klines, trades).map((p) => ({ time: p.time, value: p.equity })));
  state.equityChart.timeScale().fitContent();

  renderTrades(trades);
  elements.tradeLogCaption.textContent = `共 ${trades.length} 筆交易｜API: ${state.lastApiBase || 'same-origin'}`;

  elements.live4SummaryPanel.style.display = isAll ? '' : 'none';
  if (isAll) {
    renderLive4StrategyCards(result.strategies || {});
  }
}

function buildBacktestPayload() {
  const stopTakeChecked = elements.live4StopTakeCheckbox ? elements.live4StopTakeCheckbox.checked : true;
  const riskStopChecked = elements.live4RiskStopCheckbox ? elements.live4RiskStopCheckbox.checked : true;
  const reverseExitChecked = elements.live4ReverseExitCheckbox ? elements.live4ReverseExitCheckbox.checked : true;

  return {
    start_date: elements.live4StartDate.value,
    end_date: elements.live4EndDate.value,
    contract: 'TMF',
    stop_loss_points: Number(elements.live4StopLossPoints.value || 100),
    take_profit_points: Number(elements.live4TakeProfitPoints.value || 300),
    use_stop_take: stopTakeChecked,
    initial_capital_ntd: Number(elements.live4CapitalInput.value || 100000),
    max_loss_ntd: Number(elements.live4MaxLossNtd.value || 10000),
    max_loss_pct: Number(elements.live4MaxLossPct.value || 10) / 100,
    use_risk_stop: riskStopChecked,
    oco_enabled: stopTakeChecked,
    soft_stop_enabled: stopTakeChecked,
    risk_insurance_enabled: riskStopChecked,
    reverse_signal_exit_enabled: reverseExitChecked,
  };
}

async function runBacktest() {
  clearError();

  if (!elements.live4StartDate.value || !elements.live4EndDate.value) {
    showError('請先設定起訖日期。');
    return;
  }

  setLoading(true, '回測執行中（真實逐筆資料，約需數十秒）…');

  try {
    const response = await apiRequest('/api/backtest/live_strategies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildBacktestPayload()),
    });
    const result = await parseJsonResponse(response);
    state.lastResult = result;
    renderResult(result);
    setIdleStatus(`回測完成 (${state.lastApiBase || 'same-origin'})`);
  } catch (error) {
    showError(`回測失敗：${error.message}`);
  } finally {
    setLoading(false);
  }
}

function onStrategyViewChange() {
  // 切換「檢視範圍」不需要重新打 API——四個策略的完整結果本來就都在最近一次
  // 回測結果裡，純粹是畫面過濾，秒切換不用再等幾十秒重新跑一次逐筆資料。
  if (state.lastResult) {
    renderResult(state.lastResult);
  }
}

function bindEvents() {
  elements.runButton.addEventListener('click', runBacktest);
  elements.strategyViewSelect.addEventListener('change', onStrategyViewChange);
}

async function bootstrap() {
  const token = await requireAuth();
  if (!token) return; // requireAuth 已經導去登入頁

  initializeCharts();
  updateSummary();
  bindEvents();
}

bootstrap();
