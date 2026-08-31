-- supabase/sql/2026-08-31_add_risk_params_to_user_strategy_configs.sql
-- 為 user_strategy_configs 表新增客製化風控參數與模式欄位，支援依帳號/策略客製化匹配。
alter table if exists public.user_strategy_configs
  add column if not exists stop_loss_points double precision,
  add column if not exists take_profit_points double precision,
  add column if not exists max_loss_ntd double precision,
  add column if not exists max_loss_pct double precision,
  add column if not exists exit_mode text;
