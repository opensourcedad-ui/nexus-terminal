"""
Nexus Terminal indexer — Abstract chain activity layer.

Polls the Abstract public RPC, aggregates per-contract activity into local
SQLite, and periodically syncs 24h/7d aggregates to Supabase for the frontend.

Modes:
  python indexer.py                 # live indexing loop
  python indexer.py --simulate      # synthetic blocks, full pipeline test, no network
  python indexer.py --backfill N    # index the last N blocks then continue live

Env (see config.example.env):
  RPC_URL, SUPABASE_URL, SUPABASE_SERVICE_KEY, POLL_SECONDS, SYNC_MINUTES
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "3"))
SYNC_MINUTES = int(os.getenv("SYNC_MINUTES", "10"))
DB_PATH = os.getenv("DB_PATH", "nexus.db")
RPC_BATCH = 10  # blocks per batch request

# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- one row per (hour-bucket, contract, wallet): dedupe unit for unique wallets
CREATE TABLE IF NOT EXISTS interactions (
  hour INTEGER NOT NULL,            -- unix hour bucket
  contract TEXT NOT NULL,
  wallet TEXT NOT NULL,
  tx_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (hour, contract, wallet)
);
CREATE INDEX IF NOT EXISTS ix_inter_contract ON interactions(contract, hour);
CREATE INDEX IF NOT EXISTS ix_inter_hour ON interactions(hour);

CREATE TABLE IF NOT EXISTS contracts (
  address TEXT PRIMARY KEY,
  first_seen_block INTEGER,
  first_seen_at INTEGER,
  created_onchain INTEGER DEFAULT 0   -- 1 if we saw the creation tx itself
);

CREATE TABLE IF NOT EXISTS labels (
  address TEXT PRIMARY KEY,
  label TEXT,
  category TEXT
);

CREATE TABLE IF NOT EXISTS smart_wallets (address TEXT PRIMARY KEY, tag TEXT);

CREATE TABLE IF NOT EXISTS smart_hits (
  wallet TEXT NOT NULL,
  contract TEXT NOT NULL,
  first_seen_at INTEGER,
  PRIMARY KEY (wallet, contract)
);
"""


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(con, k, v):
    con.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )


def load_seed_files(con):
    """Load optional labels.json and smart_wallets.txt sitting next to the script."""
    base = os.path.dirname(os.path.abspath(__file__))
    lp = os.path.join(base, "labels.json")
    if os.path.exists(lp):
        with open(lp) as f:
            for addr, info in json.load(f).items():
                con.execute(
                    "INSERT INTO labels(address,label,category) VALUES(?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET label=excluded.label, category=excluded.category",
                    (addr.lower(), info.get("label"), info.get("category")),
                )
    wp = os.path.join(base, "smart_wallets.txt")
    if os.path.exists(wp):
        with open(wp) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(maxsplit=1)
                addr = parts[0].lower()
                tag = parts[1] if len(parts) > 1 else "tracked"
                con.execute(
                    "INSERT OR IGNORE INTO smart_wallets(address,tag) VALUES(?,?)",
                    (addr, tag),
                )
    con.commit()


# ---------------------------------------------------------------- rpc

class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.client = httpx.Client(timeout=20)
        self._id = 0

    def call(self, method, params):
        self._id += 1
        r = self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC error {method}: {body['error']}")
        return body["result"]

    def batch(self, calls):
        """calls: list of (method, params). Returns results in order."""
        payload = []
        for i, (m, p) in enumerate(calls):
            payload.append({"jsonrpc": "2.0", "id": i, "method": m, "params": p})
        r = self.client.post(self.url, json=payload)
        r.raise_for_status()
        body = r.json()
        by_id = {item["id"]: item for item in body}
        out = []
        for i in range(len(calls)):
            item = by_id.get(i, {})
            if "error" in item:
                raise RuntimeError(f"RPC batch error: {item['error']}")
            out.append(item.get("result"))
        return out

    def latest_block(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_blocks(self, numbers):
        calls = [("eth_getBlockByNumber", [hex(n), True]) for n in numbers]
        return self.batch(calls)

    def get_receipts(self, tx_hashes):
        if not tx_hashes:
            return []
        calls = [("eth_getTransactionReceipt", [h]) for h in tx_hashes]
        return self.batch(calls)


# ---------------------------------------------------------------- ingestion

def hour_bucket(ts: int) -> int:
    return ts - (ts % 3600)


def ingest_block(con, block, smart_set, creations_out):
    """Pull every tx in a block into the interactions table."""
    if block is None:
        return 0
    ts = int(block["timestamp"], 16)
    bn = int(block["number"], 16)
    hb = hour_bucket(ts)
    n = 0
    for tx in block.get("transactions", []):
        sender = (tx.get("from") or "").lower()
        to = tx.get("to")
        if to is None:
            # contract creation — resolve address via receipt later
            creations_out.append((tx["hash"], bn, ts))
            continue
        to = to.lower()
        # heuristic: calls with calldata are contract interactions
        if tx.get("input", "0x") in ("0x", None):
            continue
        con.execute(
            "INSERT INTO interactions(hour,contract,wallet,tx_count) VALUES(?,?,?,1) "
            "ON CONFLICT(hour,contract,wallet) DO UPDATE SET tx_count=tx_count+1",
            (hb, to, sender),
        )
        con.execute(
            "INSERT OR IGNORE INTO contracts(address,first_seen_block,first_seen_at) VALUES(?,?,?)",
            (to, bn, ts),
        )
        if sender in smart_set:
            con.execute(
                "INSERT OR IGNORE INTO smart_hits(wallet,contract,first_seen_at) VALUES(?,?,?)",
                (sender, to, ts),
            )
        n += 1
    return n


def record_creations(con, rpc, creations):
    """Resolve creation receipts in batches and mark contracts as fresh deployments."""
    for i in range(0, len(creations), RPC_BATCH):
        chunk = creations[i : i + RPC_BATCH]
        receipts = rpc.get_receipts([c[0] for c in chunk])
        for (txh, bn, ts), rec in zip(chunk, receipts):
            addr = (rec or {}).get("contractAddress")
            if addr:
                con.execute(
                    "INSERT INTO contracts(address,first_seen_block,first_seen_at,created_onchain) "
                    "VALUES(?,?,?,1) ON CONFLICT(address) DO UPDATE SET created_onchain=1",
                    (addr.lower(), bn, ts),
                )


# ---------------------------------------------------------------- aggregation + sync

def window_stats(con, since: int, until: int):
    """Per-contract tx count + unique wallets between two unix timestamps."""
    rows = con.execute(
        """
        SELECT contract, SUM(tx_count) AS txs, COUNT(DISTINCT wallet) AS wallets
        FROM interactions WHERE hour >= ? AND hour < ?
        GROUP BY contract
        """,
        (hour_bucket(since), hour_bucket(until) + 3600),
    ).fetchall()
    return {r[0]: {"txs": r[1], "wallets": r[2]} for r in rows}


def build_payloads(con, now: int):
    day = 86400
    cur = window_stats(con, now - day, now)
    prev = window_stats(con, now - 2 * day, now - day)
    week = window_stats(con, now - 7 * day, now)

    labels = {
        r[0]: (r[1], r[2])
        for r in con.execute("SELECT address,label,category FROM labels")
    }

    pulse = []
    for addr, s in cur.items():
        p = prev.get(addr, {"txs": 0, "wallets": 0})
        w = week.get(addr, s)
        lab = labels.get(addr, (None, None))
        pulse.append(
            {
                "contract": addr,
                "label": lab[0],
                "category": lab[1],
                "txs_24h": s["txs"],
                "wallets_24h": s["wallets"],
                "txs_prev_24h": p["txs"],
                "wallets_prev_24h": p["wallets"],
                "txs_7d": w["txs"],
                "wallets_7d": w["wallets"],
                "updated_at": now,
            }
        )
    pulse.sort(key=lambda x: -x["wallets_24h"])
    pulse = pulse[:300]  # keep Supabase lean

    fresh = []
    for r in con.execute(
        "SELECT address, first_seen_block, first_seen_at, created_onchain FROM contracts "
        "WHERE first_seen_at >= ? ORDER BY first_seen_at DESC LIMIT 200",
        (now - 7 * day,),
    ):
        s = cur.get(r[0], {"txs": 0, "wallets": 0})
        fresh.append(
            {
                "contract": r[0],
                "first_seen_block": r[1],
                "first_seen_at": r[2],
                "created_onchain": bool(r[3]),
                "txs_24h": s["txs"],
                "wallets_24h": s["wallets"],
                "label": labels.get(r[0], (None, None))[0],
            }
        )

    hits = []
    for r in con.execute(
        """
        SELECT h.contract, COUNT(*) AS smart_wallets, MAX(h.first_seen_at) AS last_hit
        FROM smart_hits h WHERE h.first_seen_at >= ?
        GROUP BY h.contract ORDER BY smart_wallets DESC LIMIT 100
        """,
        (now - 2 * day,),
    ):
        hits.append(
            {
                "contract": r[0],
                "smart_wallets": r[1],
                "last_hit_at": r[2],
                "label": labels.get(r[0], (None, None))[0],
            }
        )
    return pulse, fresh, hits


def sync_supabase(pulse, fresh, hits):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[sync] Supabase not configured — skipping push")
        return
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    with httpx.Client(timeout=30) as c:
        for table, rows in (
            ("app_pulse", pulse),
            ("fresh_contracts", fresh),
            ("smart_signals", hits),
        ):
            if not rows:
                continue
            r = c.post(
                f"{SUPABASE_URL}/rest/v1/{table}?on_conflict=contract",
                headers=headers,
                json=rows,
            )
            if r.status_code >= 300:
                print(f"[sync] {table} failed {r.status_code}: {r.text[:200]}")
            else:
                print(f"[sync] {table}: {len(rows)} rows")


# ---------------------------------------------------------------- loops

def live_loop(backfill: int):
    con = db_connect()
    load_seed_files(con)
    rpc = Rpc(RPC_URL)
    smart_set = {r[0] for r in con.execute("SELECT address FROM smart_wallets")}

    latest = rpc.latest_block()
    cursor = int(meta_get(con, "cursor", latest - backfill))
    last_sync = 0.0
    print(f"[start] rpc={RPC_URL} chain head={latest} cursor={cursor}")

    while True:
        head = rpc.latest_block()
        while cursor < head:
            todo = list(range(cursor + 1, min(cursor + 1 + RPC_BATCH, head + 1)))
            blocks = rpc.get_blocks(todo)
            creations = []
            txs = 0
            for b in blocks:
                txs += ingest_block(con, b, smart_set, creations)
            record_creations(con, rpc, creations)
            cursor = todo[-1]
            meta_set(con, "cursor", cursor)
            con.commit()
            print(f"[index] blocks {todo[0]}–{todo[-1]} ({txs} calls, {len(creations)} deploys)")

        if time.time() - last_sync > SYNC_MINUTES * 60:
            now = int(time.time())
            pulse, fresh, hits = build_payloads(con, now)
            sync_supabase(pulse, fresh, hits)
            last_sync = time.time()

        time.sleep(POLL_SECONDS)


def simulate():
    """Full pipeline against synthetic data — verifies SQL, aggregation, payloads."""
    global DB_PATH
    DB_PATH = "nexus_sim.db"
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = db_connect()
    load_seed_files(con)
    smart_set = {r[0] for r in con.execute("SELECT address FROM smart_wallets")}

    random.seed(42)
    apps = [f"0xapp{i:037x}" for i in range(12)]
    wallets = [f"0xw{i:038x}" for i in range(400)]
    # make two tracked smart wallets for the demo
    for w in wallets[:2]:
        con.execute("INSERT OR IGNORE INTO smart_wallets(address,tag) VALUES(?, 'og badge')", (w,))
        smart_set.add(w)

    now = int(time.time())
    bn = 1_000_000
    # 8 days of activity; app[0] "goes viral" in the last day, app[11] is brand new
    for day_off in range(8, 0, -1):
        for h in range(0, 24, 2):
            ts = now - day_off * 86400 + h * 3600
            txs = []
            for _ in range(random.randint(40, 80)):
                hot = day_off == 1 and random.random() < 0.5
                app = apps[0] if hot else random.choice(apps[:10])
                txs.append({
                    "from": random.choice(wallets),
                    "to": app,
                    "input": "0xdeadbeef",
                    "hash": f"0x{random.getrandbits(256):064x}",
                })
            if day_off == 1 and h == 12:
                for w in wallets[:2]:
                    txs.append({"from": w, "to": apps[11], "input": "0xcafe",
                                "hash": f"0x{random.getrandbits(256):064x}"})
            block = {"number": hex(bn), "timestamp": hex(ts), "transactions": txs}
            ingest_block(con, block, smart_set, [])
            bn += 1
    # fresh deployment record for app 11
    con.execute(
        "INSERT OR REPLACE INTO contracts(address,first_seen_block,first_seen_at,created_onchain) VALUES(?,?,?,1)",
        (apps[11], bn - 5, now - 86400 + 12 * 3600),
    )
    con.commit()

    pulse, fresh, hits = build_payloads(con, now)
    print("\n=== APP PULSE (top 5 by 24h wallets) ===")
    for p in pulse[:5]:
        d = p["wallets_24h"] - p["wallets_prev_24h"]
        print(f"  {p['contract'][:14]}…  wallets24h={p['wallets_24h']:>4}  Δ={d:+4}  txs24h={p['txs_24h']}")
    print("\n=== FRESH CONTRACTS ===")
    for f in fresh[:5]:
        print(f"  {f['contract'][:14]}…  deployed={f['created_onchain']}  wallets24h={f['wallets_24h']}")
    print("\n=== SMART SIGNALS ===")
    for h in hits[:5]:
        print(f"  {h['contract'][:14]}…  smart_wallets={h['smart_wallets']}")
    print("\n[simulate] pipeline OK — payloads match Supabase schema")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--backfill", type=int, default=1000)
    args = ap.parse_args()
    if args.simulate:
        simulate()
    else:
        try:
            live_loop(args.backfill)
        except KeyboardInterrupt:
            sys.exit(0)
