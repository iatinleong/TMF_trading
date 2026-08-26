// frontend/js/strategy-config.js — 帳號↔策略配對第一步（見
// docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）：
// 一般使用者只能看/開關「已經綁定給自己」的策略；管理員（is_admin）另外看到
// 一個面板，可以幫任何帳號新增/更新策略客製化設定。enabled 目前只是記錄
// 使用者的意圖，還不會觸發多帳號並發下單——那是後續計畫（Plan B），要等
// 「一台機器同時並發多個不同帳號 SKCOM 連線」實測確認可行之後才會做。

const STRATEGY_DIRECTION_LABEL = { long: '只做多', short: '只做空' };
const STRATEGY_KIND_LABEL = { breakout: '突破', pullback: '拉回' };

function rowHtml(row) {
  return `
    <div class="strategy-card" data-strategy-id="${row.strategy_id}">
      <strong>${row.strategy_id}</strong>
      <span class="stat-label">商品代碼 ${row.product_code}　口數 ${row.qty}</span>
      <label class="field checkbox-field">
        <input type="checkbox" class="row-enabled" ${row.enabled ? 'checked' : ''} />
        <span>啟用</span>
      </label>
    </div>
  `;
}

async function loadMyStrategies() {
  const res = await apiFetch('/api/my-strategy-configs');
  const rows = await res.json();

  const listEl = document.getElementById('my-strategy-list');
  const emptyEl = document.getElementById('my-strategy-empty');
  if (rows.length === 0) {
    listEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
    return;
  }
  emptyEl.classList.add('hidden');
  listEl.innerHTML = rows.map(rowHtml).join('');
  listEl.querySelectorAll('.strategy-card').forEach((card) => {
    card.querySelector('.row-enabled').addEventListener('change', (evt) => {
      toggleStrategy(card.dataset.strategyId, evt.target.checked, evt.target);
    });
  });
}

async function toggleStrategy(strategyId, enabled, checkboxEl) {
  const errorBanner = document.getElementById('error-banner');
  errorBanner.classList.add('hidden');
  try {
    const res = await apiFetch('/api/my-strategy-configs/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy_id: strategyId, enabled }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '更新失敗');
    }
  } catch (e) {
    checkboxEl.checked = !enabled; // 失敗要把畫面狀態改回去，不能讓使用者以為切換成功了
    errorBanner.textContent = e.message;
    errorBanner.classList.remove('hidden');
  }
}

async function setupAdminPanel() {
  document.getElementById('admin-panel').classList.remove('hidden');

  const defsRes = await apiFetch('/api/admin/strategy-defs');
  const defs = await defsRes.json();
  const select = document.getElementById('admin-strategy-select');
  select.innerHTML = defs
    .map((d) => `<option value="${d.strategy_id}">${d.label}（${STRATEGY_KIND_LABEL[d.strategy]}／${STRATEGY_DIRECTION_LABEL[d.direction_limit]}）</option>`)
    .join('');

  document.getElementById('admin-save-btn').addEventListener('click', async () => {
    const errorBanner = document.getElementById('error-banner');
    errorBanner.classList.add('hidden');
    const body = {
      user_id: document.getElementById('admin-user-id').value.trim(),
      strategy_id: select.value,
      product_code: document.getElementById('admin-product-code').value.trim(),
      qty: parseInt(document.getElementById('admin-qty').value, 10),
      enabled: document.getElementById('admin-enabled').checked,
    };
    try {
      const res = await apiFetch('/api/admin/strategy-configs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '儲存失敗');
      }
      await loadMyStrategies(); // 萬一管理員幫自己帳號改設定，畫面要同步
    } catch (e) {
      errorBanner.textContent = e.message;
      errorBanner.classList.remove('hidden');
    }
  });
}

async function init() {
  const token = await requireAuth();
  if (!token) return;

  const meRes = await apiFetch('/api/me');
  const me = await meRes.json();
  document.getElementById('me-email').textContent = me.email || me.user_id || '—';

  await loadMyStrategies();
  if (me.is_admin) {
    await setupAdminPanel();
  }
}

init().catch((e) => {
  const errorBanner = document.getElementById('error-banner');
  errorBanner.textContent = e.message;
  errorBanner.classList.remove('hidden');
});
