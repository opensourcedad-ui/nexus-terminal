-- Run in Supabase SQL editor. Frontend reads with anon key (read-only RLS).
create table if not exists app_pulse (
  contract text primary key,
  label text, category text,
  txs_24h int, wallets_24h int,
  txs_prev_24h int, wallets_prev_24h int,
  txs_7d int, wallets_7d int,
  vol_usd_24h double precision default 0,
  vol_usd_prev_24h double precision default 0,
  vol_usd_7d double precision default 0,
  updated_at bigint
);
-- Upgrading an existing project? Run just these three lines:
alter table app_pulse add column if not exists vol_usd_24h double precision default 0;
alter table app_pulse add column if not exists vol_usd_prev_24h double precision default 0;
alter table app_pulse add column if not exists vol_usd_7d double precision default 0;
create table if not exists fresh_contracts (
  contract text primary key,
  first_seen_block bigint, first_seen_at bigint,
  created_onchain boolean, txs_24h int, wallets_24h int, label text
);
create table if not exists smart_signals (
  contract text primary key,
  smart_wallets int, last_hit_at bigint, label text
);
alter table app_pulse enable row level security;
alter table fresh_contracts enable row level security;
alter table smart_signals enable row level security;
create policy "public read" on app_pulse for select using (true);
create policy "public read" on fresh_contracts for select using (true);
create policy "public read" on smart_signals for select using (true);
