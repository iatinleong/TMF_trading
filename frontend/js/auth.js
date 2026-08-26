// 2026-08-21：登入改用 Supabase（取代原本網址帶 ?key= 的共用金鑰）。
// SUPABASE_URL/PUBLISHABLE_KEY 是設計上可以公開放在前端的值（不是密鑰），
// 真正的驗證靠後端拿 JWKS 驗簽章，見 backend/supabase_auth.py。
const SUPABASE_URL = 'https://tgpsxzuyoqqjmwsqjiej.supabase.co';
const SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_V2J-kIZJwDhsPOrs9nbmdg_8tBsNXs6';

const AUTH_CLIENT = window.supabase.createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);

// 2026-08-24 實測抓到的競態：supabase-js 在同一個分頁裡連續呼叫 getSession()，
// 有時候第一次拿得到 session、緊接著下一次（幾百毫秒內）卻拿到 null，是
// supabase-js 已知的時序問題（見 supabase/supabase#12522、
// supabase-js#1560），不是我們自己邏輯的錯。後端 log 證實了這點：401 發生時
// 完全沒有簽章驗證失敗的錯誤（我們自己有記那個 log），代表送過去的 token
// 根本是空的——是前端這裡 getSession() 那一刻剛好回傳 null，不是 token 本身
// 無效。解法：拿到 token 後快取起來，不要每次呼叫都重新問 supabase-js，
// 減少踩到這個競態的機會；真的被後端 401 了，才強制重新問一次 session 再重試
// 一次，兩次都失敗才真的導去登入頁。
let _cachedToken = null;

async function getAccessToken(forceRefresh) {
  if (_cachedToken && !forceRefresh) return _cachedToken;
  const { data } = await AUTH_CLIENT.auth.getSession();
  _cachedToken = data && data.session ? data.session.access_token : null;
  return _cachedToken;
}

// 頁面載入時呼叫：沒有有效登入就導去登入頁，回傳目前的 access token。
async function requireAuth() {
  const token = await getAccessToken();
  if (!token) {
    window.location.href = 'login.html';
    return null;
  }
  return token;
}

// 取代直接呼叫 fetch() 打自家 API 的地方，自動帶上目前的登入 token；
// 拿到 401 先強制重新問一次 session 重試一次（見上面競態說明），還是失敗
// 才導去登入頁，不留在畫面上一直失敗。
async function apiFetch(url, options) {
  let token = await getAccessToken();
  if (!token) {
    window.location.href = 'login.html';
    throw new Error('尚未登入');
  }
  const opts = options || {};
  const doFetch = (tok) =>
    fetch(url, Object.assign({}, opts, {
      headers: Object.assign({}, opts.headers, { Authorization: `Bearer ${tok}` }),
    }));

  let res = await doFetch(token);
  if (res.status === 401) {
    token = await getAccessToken(true);
    if (token) {
      res = await doFetch(token);
    }
    if (!token || res.status === 401) {
      window.location.href = 'login.html';
      // 2026-08-21 實測抓到的 bug：這裡原本只導頁、沒有 throw，call site 會
      // 繼續把 401 的錯誤內容（一個物件，不是陣列）當成正常資料去解析，
      // 導致 loadKlines() 裡 klinesCache.map 直接炸掉，畫面看起來像卡住/狂跳轉。
      // throw 讓呼叫端用既有的 try/catch 或 init().catch() 乾淨地停下來。
      throw new Error('登入已過期，請重新登入');
    }
  }
  return res;
}

async function logout() {
  _cachedToken = null;
  await AUTH_CLIENT.auth.signOut();
  window.location.href = 'login.html';
}
