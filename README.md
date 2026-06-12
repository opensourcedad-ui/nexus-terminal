# Nexus Terminal — the live activity layer for Abstract

Real onchain usage for every Abstract app: wallets, transactions, fresh
deployments, and OG-wallet movements. No votes, no noise.

## Architecture
```
Abstract RPC ──► worker/indexer.py (VPS) ──► Supabase ──► frontend (Vercel)
                 SQLite (raw dedupe)         3 small tables   reads anon key
```

## Deploy (≈30 min)

### 1. Supabase (free tier)
- New project → SQL editor → paste `worker/schema.sql` → run.
- Copy project URL + anon key + service-role key.

### 2. Worker (your VPS)
```bash
cd worker
pip install -r requirements.txt --break-system-packages
cp config.example.env .env   # fill in Supabase URL + service key
python indexer.py --simulate # sanity check, no network needed
python indexer.py --backfill 5000   # go live (≈1.5h of Abstract blocks)
```
Run under systemd or `tmux`. It checkpoints its cursor in SQLite and
resumes where it left off after restarts.

### 3. Frontend (Vercel free tier)
```bash
cd frontend
cp .env.example .env   # fill in Supabase URL + ANON key (never service key)
npm install && npm run build   # or just `vercel` and set env vars in dashboard
```
Without env vars the site renders demo data — useful for previewing design.

## Tuning
- `worker/labels.json` — map contract → app name/category. Bootstrap from the
  Portal app list; this mapping IS the product moat, grow it daily.
- `worker/smart_wallets.txt` — seed with top badge holders / OG NFT wallets.
- Storage: interactions table grows ~per (hour,contract,wallet). Prune rows
  older than 8 days with a daily cron:
  `sqlite3 nexus.db "DELETE FROM interactions WHERE hour < strftime('%s','now')-691200"`

## Honest limitations (v1)
- "First seen" contracts use a calldata heuristic; some EOAs with data txs may
  appear until labeled. Creation-receipt deploys are exact.
- Backfill depth is limited by public RPC patience. 5–10k blocks is fine;
  full history needs an archive node or paid RPC (later problem).
- Verify current Abstract RPC rate limits before cranking RPC_BATCH up.
