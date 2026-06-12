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

# Priced tokens on Abstract mainnet: address -> (symbol, decimals).
# ERC-20 Transfer events of these tokens are converted to USD and credited
# to the contract the user called. Native ETH value is priced via WETH.
TOKENS = {
    "0x3439153eb7af838ad19d56e1571fbd09333c2809": ("WETH", 18),
    "0x9ebe3a824ca958e4b3da772d2065518f009cba62": ("PENGU", 18),
    "0x84a71ccd554cc1b02749b35d22f684cc8ec987e1": ("USDC.e", 6),
}
WETH_ADDR = "0x3439153eb7af838ad19d56e1571fbd09333c2809"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PRICE_TTL = 600  # seconds

# Auto-labeling: verified contract names from Etherscan V2 (chainid 2741)
ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")
PROXY_NAMES = {
    "ERC1967Proxy", "TransparentUpgradeableProxy", "AdminUpgradeabilityProxy",
    "BeaconProxy", "Proxy", "UUPSProxy",
}
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
AUTOLABEL_PER_SYNC = 15
AUTOLABEL_RETRY = 86400  # re-check unverified contracts daily

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

-- USD volume that moved through each contract, per hour bucket
CREATE TABLE IF NOT EXISTS volumes (
  hour INTEGER NOT NULL,
  contract TEXT NOT NULL,
  usd REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, contract)
);
CREATE INDEX IF NOT EXISTS ix_vol_hour ON volumes(hour);

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


# ---------------------------------------------------------------- pricing

class Prices:
    """USD prices from DexScreener, cached PRICE_TTL seconds. Falls back to the
    last known price (persisted in the meta table) if the API is unreachable."""

    def __init__(self, con, fixed=None):
        self.con = con
        self.fixed = fixed  # {addr: price} for simulate mode
        self.cache = {}     # addr -> (price, fetched_at)

    def get(self, token: str) -> float:
        token = token.lower()
        if self.fixed is not None:
            return self.fixed.get(token, 0.0)
        hit = self.cache.get(token)
        if hit and time.time() - hit[1] < PRICE_TTL:
            return hit[0]
        price = self._fetch(token)
        if price is None:
            stale = meta_get(self.con, f"price:{token}")
            price = float(stale) if stale else 0.0
        else:
            meta_set(self.con, f"price:{token}", price)
        self.cache[token] = (price, time.time())
        return price

    def _fetch(self, token):
        try:
            r = httpx.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=15
            )
            r.raise_for_status()
            pairs = [
                p for p in (r.json().get("pairs") or [])
                if p.get("chainId") == "abstract"
                and p["baseToken"]["address"].lower() == token
                and p.get("priceUsd")
            ]
            if not pairs:
                return None
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
            return float(best["priceUsd"])
        except Exception as e:
            print(f"[price] {token[:10]}… fetch failed: {e}")
            return None


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

    def get_logs(self, from_block, to_block, addresses, topic):
        return self.call(
            "eth_getLogs",
            [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": addresses,
                "topics": [topic],
            }],
        )


# ---------------------------------------------------------------- ingestion

def hour_bucket(ts: int) -> int:
    return ts - (ts % 3600)


def add_volume(con, hour: int, contract: str, usd: float):
    if usd <= 0:
        return
    con.execute(
        "INSERT INTO volumes(hour,contract,usd) VALUES(?,?,?) "
        "ON CONFLICT(hour,contract) DO UPDATE SET usd=usd+excluded.usd",
        (hour, contract, usd),
    )


def ingest_block(con, block, smart_set, creations_out, prices, txmap=None):
    """Pull every tx in a block into the interactions table."""
    if block is None:
        return 0
    ts = int(block["timestamp"], 16)
    bn = int(block["number"], 16)
    hb = hour_bucket(ts)
    eth_price = prices.get(WETH_ADDR)
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
        if txmap is not None:
            txmap[tx["hash"]] = (hb, to)
        wei = int(tx.get("value", "0x0"), 16)
        if wei:
            add_volume(con, hb, to, wei / 1e18 * eth_price)
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


def ingest_token_volume(con, rpc, from_block, to_block, txmap, prices):
    """Credit ERC-20 transfers of priced tokens to the contract the user called."""
    if not txmap:
        return
    try:
        logs = rpc.get_logs(from_block, to_block, list(TOKENS), TRANSFER_TOPIC)
    except Exception as e:
        print(f"[vol] getLogs {from_block}-{to_block} failed: {e}")
        return
    for log in logs:
        entry = txmap.get(log.get("transactionHash"))
        if entry is None:
            continue
        hb, app = entry
        token = log["address"].lower()
        sym, dec = TOKENS[token]
        amount = int(log.get("data", "0x0"), 16) / 10**dec
        add_volume(con, hb, app, amount * prices.get(token))


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


# ---------------------------------------------------------------- auto-labeling

def prettify(name: str) -> str:
    """MoodyMadnessAssets -> Moody Madness Assets (keeps V3, Router02 intact)."""
    import re
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip()


def etherscan_name(client, address: str):
    r = client.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": "2741", "module": "contract", "action": "getsourcecode",
            "address": address, "apikey": ETHERSCAN_KEY,
        },
    )
    res = r.json().get("result")
    if isinstance(res, list) and res:
        return res[0].get("ContractName") or None
    return None


def auto_label(con, rpc, now: int):
    """Label the most-used unlabeled contracts with their verified contract name.
    Proxies are resolved through the EIP-1967 implementation slot. Unverified
    contracts are retried daily. Curated labels.json entries always win."""
    if not ETHERSCAN_KEY:
        return
    candidates = con.execute(
        """
        SELECT i.contract, COUNT(DISTINCT i.wallet) AS w FROM interactions i
        LEFT JOIN labels l ON l.address = i.contract
        WHERE i.hour >= ? AND l.address IS NULL
        GROUP BY i.contract ORDER BY w DESC LIMIT 60
        """,
        (hour_bucket(now - 86400),),
    ).fetchall()
    done = 0
    with httpx.Client(timeout=15) as c:
        for addr, _w in candidates:
            if done >= AUTOLABEL_PER_SYNC:
                break
            tried = meta_get(con, f"lbl_tried:{addr}")
            if tried and now - int(tried) < AUTOLABEL_RETRY:
                continue
            done += 1
            meta_set(con, f"lbl_tried:{addr}", now)
            try:
                name = etherscan_name(c, addr)
                if name in PROXY_NAMES:
                    slot = rpc.call("eth_getStorageAt", [addr, IMPL_SLOT, "latest"])
                    impl = "0x" + slot[-40:]
                    if int(impl, 16):
                        name = etherscan_name(c, impl) or None
                    else:
                        name = None
                if name and name not in PROXY_NAMES:
                    con.execute(
                        "INSERT INTO labels(address,label,category) VALUES(?,?,NULL) "
                        "ON CONFLICT(address) DO NOTHING",
                        (addr, prettify(name)),
                    )
                    print(f"[label] {addr[:10]}… -> {prettify(name)}")
            except Exception as e:
                print(f"[label] {addr[:10]}… failed: {e}")
            time.sleep(0.25)
    con.commit()


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


def window_volume(con, since: int, until: int):
    rows = con.execute(
        "SELECT contract, SUM(usd) FROM volumes WHERE hour >= ? AND hour < ? GROUP BY contract",
        (hour_bucket(since), hour_bucket(until) + 3600),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def build_payloads(con, now: int):
    day = 86400
    cur = window_stats(con, now - day, now)
    prev = window_stats(con, now - 2 * day, now - day)
    week = window_stats(con, now - 7 * day, now)
    vol_cur = window_volume(con, now - day, now)
    vol_prev = window_volume(con, now - 2 * day, now - day)
    vol_week = window_volume(con, now - 7 * day, now)

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
                "vol_usd_24h": round(vol_cur.get(addr, 0.0), 2),
                "vol_usd_prev_24h": round(vol_prev.get(addr, 0.0), 2),
                "vol_usd_7d": round(vol_week.get(addr, 0.0), 2),
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
            if r.status_code >= 300 and "PGRST204" in r.text:
                # volume columns not added in Supabase yet — sync without them
                slim = [{k: v for k, v in row.items() if not k.startswith("vol_")} for row in rows]
                r = c.post(
                    f"{SUPABASE_URL}/rest/v1/{table}?on_conflict=contract",
                    headers=headers,
                    json=slim,
                )
                if r.status_code < 300:
                    print(f"[sync] {table}: {len(rows)} rows (volume columns missing in Supabase — run the ALTER)")
                    continue
            if r.status_code >= 300:
                print(f"[sync] {table} failed {r.status_code}: {r.text[:200]}")
            else:
                print(f"[sync] {table}: {len(rows)} rows")


# ---------------------------------------------------------------- loops

def prune(con, now: int):
    """Keep interactions 9 days (powers 7d windows), volumes 35 days (monthly later)."""
    con.execute("DELETE FROM interactions WHERE hour < ?", (now - 9 * 86400,))
    con.execute("DELETE FROM volumes WHERE hour < ?", (now - 35 * 86400,))
    con.commit()


def live_loop(backfill: int):
    con = db_connect()
    load_seed_files(con)
    rpc = Rpc(RPC_URL)
    prices = Prices(con)
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
            txmap = {}
            txs = 0
            for b in blocks:
                txs += ingest_block(con, b, smart_set, creations, prices, txmap)
            ingest_token_volume(con, rpc, todo[0], todo[-1], txmap, prices)
            record_creations(con, rpc, creations)
            cursor = todo[-1]
            meta_set(con, "cursor", cursor)
            con.commit()
            print(f"[index] blocks {todo[0]}–{todo[-1]} ({txs} calls, {len(creations)} deploys)")

        if time.time() - last_sync > SYNC_MINUTES * 60:
            now = int(time.time())
            prune(con, now)
            auto_label(con, rpc, now)
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
    prices = Prices(con, fixed={WETH_ADDR: 1700.0})

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
                    "value": hex(random.randint(0, 5 * 10**16)),
                    "hash": f"0x{random.getrandbits(256):064x}",
                })
            if day_off == 1 and h == 12:
                for w in wallets[:2]:
                    txs.append({"from": w, "to": apps[11], "input": "0xcafe",
                                "hash": f"0x{random.getrandbits(256):064x}"})
            block = {"number": hex(bn), "timestamp": hex(ts), "transactions": txs}
            ingest_block(con, block, smart_set, [], prices)
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
        print(f"  {p['contract'][:14]}…  wallets24h={p['wallets_24h']:>4}  Δ={d:+4}  txs24h={p['txs_24h']}  vol24h=${p['vol_usd_24h']:,.0f}")
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
