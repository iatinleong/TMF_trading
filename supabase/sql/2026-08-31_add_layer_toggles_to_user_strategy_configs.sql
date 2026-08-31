-- supabase/sql/2026-08-31_add_layer_toggles_to_user_strategy_configs.sql
-- 為 user_strategy_configs 表新增四道出場防護網的獨立開關欄位，可依帳號/
-- 策略客製化關閉；not null default true 確保既有資料列與未來新插入的列，
-- 沒指定值時一律視為「開啟」，跟 StrategyState 的 Python 端預設值一致。
alter table if exists public.user_strategy_configs
  add column if not exists oco_enabled boolean not null default true,
  add column if not exists soft_stop_enabled boolean not null default true,
  add column if not exists risk_insurance_enabled boolean not null default true,
  add column if not exists reverse_signal_exit_enabled boolean not null default true;
