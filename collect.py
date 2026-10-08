"""
Early Hunter - cloud collector (runs on GitHub every ~15 minutes)

1) loads saved data from the data/ folder
2) scans new Base blocks for contract deployments
3) looks up history + funding of wallets that deployed several contracts
4) builds explainable candidates and writes docs/data.json for the website
"""

import csv
import json
import os
import sqlite3
import time

import analyze
import enrich
import step3_collect as c

CSV_FILE = "data/deployments.csv"
STATE_FILE = "data/state.json"
WALLETS_FILE = "data/wallets.csv"
FUNDERS_FILE = "data/funders.csv"
SEEN_FILE = "data/candidates_seen.csv"
SITE_JSON = "docs/data.json"
KEEP_DAYS = 7
HEADER = ["contract", "creator", "block", "timestamp", "tx_hash"]


def load(db):
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) == 5:
                    db.execute("INSERT OR IGNORE INTO deployments VALUES (?,?,?,?,?)",
                               (row[0], row[1], int(row[2]), int(row[3]), row[4]))
        db.commit()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f).get("last_block")
        if saved is not None:
            c.set_last_block(db, saved)


def dump(db):
    rows = db.execute("SELECT contract, creator, block, timestamp, tx_hash FROM deployments ORDER BY block, contract").fetchall()
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    last = c.get_last_block(db)
    if last is not None:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_block": last}, f)


def load_seen():
    seen = {}
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, newline="") as f:
            for r in csv.DictReader(f):
                seen[r["id"]] = int(r["first_seen"])
    return seen


def save_seen(seen):
    with open(SEEN_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "first_seen"])
        for k in sorted(seen):
            w.writerow([k, seen[k]])


def research_table(db):
    out = []
    for creator, n, t_min, t_max in db.execute(
        """SELECT creator, COUNT(*) n, MIN(timestamp), MAX(timestamp) FROM deployments
           GROUP BY creator HAVING n >= 2 ORDER BY n DESC, MAX(timestamp) DESC LIMIT 100"""
    ).fetchall():
        contracts = db.execute(
            "SELECT contract, timestamp, tx_hash FROM deployments WHERE creator=? ORDER BY timestamp DESC LIMIT 20",
            (creator,)).fetchall()
        out.append({"creator": creator, "count": n, "first": t_min, "last": t_max,
                    "contracts": [{"address": a, "time": t, "tx": h} for a, t, h in contracts]})
    return out


def main():
    started = time.time()
    os.makedirs("data", exist_ok=True)
    os.makedirs("docs", exist_ok=True)

    c.DB_FILE = ":memory:"
    c.FIRST_RUN_BLOCKS = 600
    c.MAX_BLOCKS_PER_RUN = 12000
    c.DEADLINE = started + 20 * 60

    db = c.open_db()
    load(db)

    try:
        latest = int(c.rpc("eth_blockNumber", []), 16)
        last = c.get_last_block(db)
        if last is not None and latest - last > 20000:
            c.set_last_block(db, latest - c.FIRST_RUN_BLOCKS)
        c.scan(db)
    except Exception as error:
        print(f"Scan stopped early: {error}")

    now = int(time.time())
    db.execute("DELETE FROM deployments WHERE timestamp < ?", (now - KEEP_DAYS * 86400,))
    db.commit()
    dump(db)

    # ---- wallet enrichment
    wallets = enrich.read_rows(WALLETS_FILE)
    funders = enrich.read_rows(FUNDERS_FILE)
    rows = db.execute(
        """SELECT creator, MIN(timestamp), MAX(timestamp) FROM deployments
           GROUP BY creator HAVING COUNT(*) >= 2 ORDER BY MAX(timestamp) DESC"""
    ).fetchall()
    targets = []
    for creator, first_ts, _ in rows:
        r = wallets.get(creator)
        if r is None or (r.get("status") == "error" and now - int(r.get("enriched_at") or 0) > 6 * 3600):
            targets.append((creator, first_ts))
    stats = enrich.enrich_all(targets, wallets, funders, started + 32 * 60, max_new=100)
    print(f"Enrichment: {stats}")

    creators = {r[0] for r in db.execute("SELECT DISTINCT creator FROM deployments").fetchall()}
    wallets = {a: r for a, r in wallets.items() if a in creators}
    used = {r.get("funder") for r in wallets.values() if r.get("funder")}
    funders = {a: r for a, r in funders.items() if a in used}
    enrich.write_rows(WALLETS_FILE, enrich.WALLET_FIELDS, wallets)
    enrich.write_rows(FUNDERS_FILE, enrich.FUNDER_FIELDS, funders)

    # ---- candidates
    deployments = db.execute("SELECT contract, creator, block, timestamp, tx_hash FROM deployments").fetchall()
    seen = load_seen()
    candidates, seen = analyze.build_candidates(deployments, wallets, funders, seen, now)
    save_seen(seen)

    out = {
        "updated": now,
        "total_contracts": len(deployments),
        "total_wallets": len(creators),
        "last_block": c.get_last_block(db),
        "window_days": KEEP_DAYS,
        "enrichment": {"analysed": sum(1 for r in wallets.values() if r.get("status") in ("ok", "heavy", "empty")),
                       "waiting": max(0, len(targets) - stats["done"]),
                       "errors": stats["errors"], "last_error": stats["error_message"]},
        "candidates": candidates,
        "wallets": research_table(db),
    }
    with open(SITE_JSON, "w") as f:
        json.dump(out, f)
    print(f"Saved. {len(candidates)} candidates.")


if __name__ == "__main__":
    main()
