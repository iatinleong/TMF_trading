async function doLogin() {
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  const msg = document.getElementById('login-msg');
  const btn = document.getElementById('login-btn');
  if (!email || !password) {
    msg.className = 'order-msg err';
    msg.textContent = '請輸入帳號密碼';
    return;
  }
  btn.disabled = true;
  msg.className = 'order-msg';
  msg.textContent = '登入中…';
  try {
    const { error } = await AUTH_CLIENT.auth.signInWithPassword({ email, password });
    if (error) {
      msg.className = 'order-msg err';
      msg.textContent = error.message || '登入失敗';
      return;
    }
    window.location.href = 'index.html';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('login-btn').addEventListener('click', doLogin);
document.getElementById('login-password').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') doLogin();
});

// 已經是登入狀態的話（例如瀏覽器分頁還留著 session）就直接跳過登入頁。
(async () => {
  const { data } = await AUTH_CLIENT.auth.getSession();
  if (data && data.session) window.location.href = 'index.html';
})();
