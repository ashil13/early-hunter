"""
Early Hunter - Step 3
1) Scans recent Base blocks for new contract deployments
2) Saves them in a local database file (early_hunter.db)
3) Shows creators (wallets) that deployed MORE THAN ONE contract

You can run it again and again: it continues from where it stopped.
"""

import sqlite3
import time
from datetime import datetime, timezone

import requests

RPC_URL = "https://mainnet.base.org"
DB_FILE = "early_hunter.db"
FIRST_RUN_BLOCKS = 1800  # about 1 hour of Base on the first run
MAX_BLOCKS_PER_RUN = 3000  # safety limit per run
MIN_CONTRACTS_TO_REPORT = 2
DEADLINE = None  # optional: unix time after which scanning stops (used by the cloud collector)
SESSION = requests.Session()  # re-uses the connection, faster than a new one each time


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(4):
        try:
            response = SESSION.post(RPC_URL, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except Exception as error:
            if attempt == 3:
                raise
            print(f"  (retrying after error: {error})")
            time.sleep(3)


def open_db():
    db = sqlite3.connect(DB_FILE)
    db.execute(
        """CREATE TABLE IF NOT EXISTS deployments (
            contract   TEXT PRIMARY KEY,
            creator    TEXT NOT NULL,
            block      INTEGER NOT NULL,
            timestamp  INTEGER NOT NULL,
            tx_hash    TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_creator ON deployments(creator)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db.commit()
    return db


def get_last_block(db):
    row = db.execute("SELECT value FROM meta WHERE key='last_block'").fetchone()
    return int(row[0]) if row else None


def set_last_block(db, number):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_block', ?)", (str(number),))
    db.commit()


def scan(db):
    latest = int(rpc("eth_blockNumber", []), 16)
    last = get_last_block(db)
    start = (latest - FIRST_RUN_BLOCKS + 1) if last is None else last + 1
    end = min(latest, start + MAX_BLOCKS_PER_RUN - 1)

    if start > end:
        print("Nothing new to scan yet.")
        return

    print(f"Scanning blocks {start} to {end} ({end - start + 1} blocks). This can take a few minutes...")
    new_count = 0
    last_done = start - 1
    for number in range(start, end + 1):
        if DEADLINE and time.time() > DEADLINE:
            print("Time budget used up - stopping here, the rest will be scanned next run.")
            break
        block = rpc("eth_getBlockByNumber", [hex(number), True])
        ts = int(block["timestamp"], 16)
        for tx in block["transactions"]:
            if tx["to"] is None:
                receipt = rpc("eth_getTransactionReceipt", [tx["hash"]])
                contract = receipt.get("contractAddress")
                if contract and receipt.get("status") == "0x1":
                    cur = db.execute(
                        "INSERT OR IGNORE INTO deployments VALUES (?, ?, ?, ?, ?)",
                        (contract.lower(), tx["from"].lower(), number, ts, tx["hash"]),
                    )
                    new_count += cur.rowcount
        last_done = number
        if (number - start) % 100 == 0:
            db.commit()
            set_last_block(db, number)
            print(f"  ... block {number} done ({end - number} left), new contracts so far: {new_count}")
        time.sleep(0.05)

    db.commit()
    if last_done >= start:
        set_last_block(db, last_done)
    print(f"Finished. Saved {new_count} new contracts.\n")


def report(db):
    total = db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
    wallets = db.execute("SELECT COUNT(DISTINCT creator) FROM deployments").fetchone()[0]
    print(f"Database now has {total} contracts from {wallets} different creator wallets.\n")

    rows = db.execute(
        """SELECT creator, COUNT(*) AS n, MIN(timestamp), MAX(timestamp)
           FROM deployments GROUP BY creator
           HAVING n >= ? ORDER BY n DESC LIMIT 15""",
        (MIN_CONTRACTS_TO_REPORT,),
    ).fetchall()

    if not rows:
        print("No wallet has deployed several contracts yet. Run again later to collect more.")
        return

    print(f"Wallets that deployed {MIN_CONTRACTS_TO_REPORT}+ contracts (top 15):")
    print("-" * 70)
    for creator, n, t_min, t_max in rows:
        minutes = (t_max - t_min) / 60
        first = datetime.fromtimestamp(t_min, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{creator}")
        print(f"   contracts: {n}   first: {first} UTC   spread over: {minutes:.0f} min")
        print(f"   https://basescan.org/address/{creator}")
    print("-" * 70)
    print("Tip: open a link above and look at what this wallet does.")


def main():
    db = open_db()
    scan(db)
    report(db)
    db.close()


if __name__ == "__main__":
    main()
