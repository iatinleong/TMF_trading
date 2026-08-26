-- supabase/sql/2026-08-26_user_strategy_configs.sql
-- 帳號 ↔ 策略配對：一般使用者的策略欄位預設是空的，由管理員手動客製化綁定
-- （見 docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。
create table if not exists public.user_strategy_configs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  strategy_id text not null,
  product_code text not null default 'TM2608',
  qty integer,
  enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, strategy_id)
);

alter table public.user_strategy_configs enable row level security;
-- 刻意不建任何 policy：跟 app_secrets 一樣，只有拿 service_role/
-- SUPABASE_SECRET_KEY 的後端能讀寫，前端連不到這張表。資料歸屬跟權限完全靠
-- backend/api.py 把關（一般使用者只能讀/切換自己的列，新增/改參數限管理員），
-- 不靠 RLS policy。

-- 把「這個帳號」現在已經在跑的 4 個策略種成初始資料，代表現況。
insert into public.user_strategy_configs (user_id, strategy_id, product_code, qty, enabled)
values
  ('01e79d6d-a686-4588-bdd2-5329512b97ef', 'breakout_long', 'TM2608', 1, true),
  ('01e79d6d-a686-4588-bdd2-5329512b97ef', 'breakout_short', 'TM2608', 1, true),
  ('01e79d6d-a686-4588-bdd2-5329512b97ef', 'pullback_long', 'TM2608', 1, true),
  ('01e79d6d-a686-4588-bdd2-5329512b97ef', 'pullback_short', 'TM2608', 1, true)
on conflict (user_id, strategy_id) do nothing;
