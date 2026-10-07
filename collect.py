"""
Early Hunter - cloud collector (runs on GitHub every 10 minutes)

1) loads saved data from data/deployments.csv
2) scans new Base blocks
3) saves everything back + writes docs/data.json for the website
"""

import csv
import json
import os
import sqlite3
import time

import step3_collect as c

CSV_FILE = "data/deployments.csv"
STATE_FILE = "data/state.json"
SITE_JSON = "docs/data.json"
KEEP_DAYS = 7  # old rows are dropped so the files stay small
HEADER = ["contract", "creator", "block", "timestamp", "tx_hash"]


def load(db):
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) == 5:
                    db.execute(
                        "INSERT OR IGNORE INTO deployments VALUES (?,?,?,?,?)",
                        (row[0], row[1], int(row[2]), int(row[3]), row[4]),
                    )
        db.commit()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f).get("last_block")
        if saved is not None:
            c.set_last_block(db, saved)


def dump(db):
    rows = db.execute(
        "SELECT contract, creator, block, timestamp, tx_hash FROM deployments ORDER BY block, contract"
    ).fetchall()
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    last = c.get_last_block(db)
    if last is not None:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_block": last}, f)


def build_json(db):
    wallets = []
    for creator, n, t_min, t_max in db.execute(
        """SELECT creator, COUNT(*) n, MIN(timestamp), MAX(timestamp)
           FROM deployments GROUP BY creator HAVING n >= 2
           ORDER BY n DESC, MAX(timestamp) DESC LIMIT 200"""
    ).fetchall():
        contracts = db.execute(
            """SELECT contract, timestamp, tx_hash FROM deployments
               WHERE creator = ? ORDER BY timestamp DESC LIMIT 50""",
            (creator,),
        ).fetchall()
        wallets.append(
            {
                "creator": creator,
                "count": n,
                "first": t_min,
                "last": t_max,
                "contracts": [{"address": a, "time": t, "tx": h} for a, t, h in contracts],
            }
        )
    return {
        "updated": int(time.time()),
        "total_contracts": db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0],
        "total_wallets": db.execute("SELECT COUNT(DISTINCT creator) FROM deployments").fetchone()[0],
        "last_block": c.get_last_block(db),
        "window_days": KEEP_DAYS,
        "wallets": wallets,
    }


def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("docs", exist_ok=True)

    c.DB_FILE = ":memory:"
    c.FIRST_RUN_BLOCKS = 600
    c.MAX_BLOCKS_PER_RUN = 2000

    db = c.open_db()
    load(db)

    try:
        latest = int(c.rpc("eth_blockNumber", []), 16)
        last = c.get_last_block(db)
        if last is not None and latest - last > 20000:  # was paused for a long time: skip the gap
            c.set_last_block(db, latest - c.FIRST_RUN_BLOCKS)
        c.scan(db)
    except Exception as error:  # still save whatever we have
        print(f"Scan stopped early: {error}")

    db.execute("DELETE FROM deployments WHERE timestamp < ?", (int(time.time()) - KEEP_DAYS * 86400,))
    db.commit()

    dump(db)
    with open(SITE_JSON, "w") as f:
        json.dump(build_json(db), f)
    print("Saved data and website file.")


if __name__ == "__main__":
    main()
