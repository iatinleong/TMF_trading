-- supabase/sql/2026-08-26_user_broker_credentials.sql
-- 每個登入帳號自己的群益期貨帳密（見
-- docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。
-- 這張表存的是真實交易密碼，比 user_strategy_configs 敏感得多——一樣啟用
-- RLS、不建 policy，只有拿 SUPABASE_SECRET_KEY 的後端能讀寫；額外要求：
-- 絕對不能有任何前端網頁或一般 API 端點讀取這張表，只有 worker 行程啟動
-- 時用 SUPABASE_SECRET_KEY 直接讀。
create table if not exists public.user_broker_credentials (
  user_id uuid primary key references auth.users(id) on delete cascade,
  capital_user_id text not null,
  capital_password text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.user_broker_credentials enable row level security;
