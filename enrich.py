"""
Early Hunter - wallet enrichment (Blockscout, free, no key)

For wallets that deployed several contracts we look up:
  - how old the wallet is and how many transactions it has
  - whether it was dormant for a long time and then "woke up"
  - who funded it (the first incoming ETH)
Everything is saved in data/wallets.csv and data/funders.csv.
"""

import csv
import os
import re
import time
from datetime import datetime

import requests

API_KEY = os.environ.get("BLOCKSCOUT_API_KEY", "").strip()
if API_KEY:
    BASE = "https://api.blockscout.com/8453/api/v2"   # Blockscout PRO API (free key), Base = chain 8453
else:
    BASE = "https://base.blockscout.com/api/v2"       # public endpoint (often blocks cloud servers)
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
})
PAUSE = 0.3          # seconds between requests (the free API is shared)
MAX_PAGES = 4        # 4 pages x 50 = 200 transactions of history
HEAVY = 200          # wallets with more transactions than this are "heavy history"
DAY = 86400

WALLET_FIELDS = ["address", "status", "tx_count", "first_tx", "wake_ts", "gap_days",
                 "first_deploy", "funder", "fund_wei", "fund_ts", "enriched_at"]
FUNDER_FIELDS = ["address", "name", "is_contract"]


class ApiError(Exception):
    pass


def clean(text):
    """Never let URLs or the API key leak into logs or the public website."""
    text = re.sub(r"https?://\S+", "<url>", str(text))
    if API_KEY:
        text = text.replace(API_KEY, "***")
    return re.sub(r"proapi_\w+", "***", text)


# ------------------------------------------------------------------ csv helpers
def read_rows(path):
    rows = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("address"):
                    rows[r["address"]] = r
    return rows


def write_rows(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for key in sorted(rows):
            w.writerow(rows[key])


# ------------------------------------------------------------------ http
def get(path, params=None):
    params = dict(params or {})
    if API_KEY:
        params["apikey"] = API_KEY
    for attempt in range(5):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=30)
            if r.status_code in (401, 402, 403):
                hint = "no BLOCKSCOUT_API_KEY set" if not API_KEY else "key sent"
                body = clean(getattr(r, "text", "") or "")[:160]
                raise ApiError(f"HTTP {r.status_code} from Blockscout ({hint}) {body}")
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(PAUSE)
            return r.json()
        except ApiError:
            raise
        except requests.RequestException as error:
            if attempt == 4:
                raise ApiError(clean(f"{type(error).__name__}: {error}"))
            time.sleep(2)
    raise ApiError("rate limited too many times")


def ts_of(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def addr_of(obj):
    return ((obj or {}).get("hash") or "").lower()


# ------------------------------------------------------------------ lookups
def fetch_history(address):
    items, params = [], {}
    for _ in range(MAX_PAGES):
        data = get(f"/addresses/{address}/transactions", params)
        if not data:
            break
        for it in data.get("items", []):
            if it.get("status") == "error":
                continue
            items.append({
                "ts": ts_of(it["timestamp"]),
                "from": addr_of(it.get("from")),
                "to": addr_of(it.get("to")),
                "value": int(it.get("value") or 0),
            })
        nxt = data.get("next_page_params")
        if not nxt:
            break
        params = nxt
    return items


def first_internal_incoming(address):
    data = get(f"/addresses/{address}/internal-transactions", {"filter": "to"})
    best = None
    for it in (data or {}).get("items", []):
        value = int(it.get("value") or 0)
        sender = addr_of(it.get("from"))
        if value > 0 and sender and sender != address:
            t = ts_of(it["timestamp"])
            if best is None or t < best["ts"]:
                best = {"ts": t, "from": sender, "value": value}
    return best


def enrich_wallet(address, first_deploy_ts):
    row = {"address": address, "status": "ok", "tx_count": "", "first_tx": "", "wake_ts": "",
           "gap_days": "", "first_deploy": first_deploy_ts, "funder": "", "fund_wei": "",
           "fund_ts": "", "enriched_at": int(time.time())}

    counters = get(f"/addresses/{address}/counters")
    count = int((counters or {}).get("transactions_count") or 0)
    row["tx_count"] = count
    if count > HEAVY:
        row["status"] = "heavy"
        return row

    txs = fetch_history(address)
    if not txs:
        row["status"] = "empty"
        return row

    times = sorted(t["ts"] for t in txs)
    row["first_tx"] = times[0]

    # "woke up": the last gap of 30+ days, followed by activity within 7 days of the first deployment
    wake = None
    for i in range(len(times) - 1, 0, -1):
        gap = times[i] - times[i - 1]
        if gap >= 30 * DAY:
            wake = (times[i], gap // DAY)
            break
    if wake and wake[0] <= first_deploy_ts and first_deploy_ts - wake[0] <= 7 * DAY:
        row["wake_ts"], row["gap_days"] = wake

    # funding: first incoming ETH (at/after the wake-up if the wallet was dormant)
    incoming = sorted(
        (t for t in txs if t["to"] == address and t["from"] != address and t["value"] > 0),
        key=lambda t: t["ts"],
    )
    if row["wake_ts"]:
        after = [t for t in incoming if t["ts"] >= int(row["wake_ts"]) - 3600]
        incoming = after or incoming
    funding = incoming[0] if incoming else first_internal_incoming(address)
    if funding:
        row["funder"], row["fund_wei"], row["fund_ts"] = funding["from"], funding["value"], funding["ts"]
    return row


def lookup_funder(address):
    data = get(f"/addresses/{address}") or {}
    name = data.get("name") or data.get("ens_domain_name") or ""
    tags = data.get("public_tags") or []
    if not name and tags:
        name = ", ".join(str(t.get("display_name") or t.get("name") or "") for t in tags[:2])
    return {"address": address, "name": name,
            "is_contract": "1" if data.get("is_contract") else "0"}


# ------------------------------------------------------------------ main entry
def enrich_all(targets, wallets, funders, deadline, max_new=100):
    """targets: list of (address, first_deploy_ts), most interesting first. Fails soft."""
    stats = {"done": 0, "errors": 0, "error_message": ""}
    consecutive = 0
    for address, first_deploy in targets:
        if stats["done"] >= max_new or time.time() > deadline:
            break
        try:
            wallets[address] = enrich_wallet(address, first_deploy)
            stats["done"] += 1
            consecutive = 0
            f = wallets[address]["funder"]
            if f and f not in funders:
                funders[f] = lookup_funder(f)
        except Exception as error:
            stats["errors"] += 1
            stats["error_message"] = clean(error)[:200]
            consecutive += 1
            wallets[address] = {"address": address, "status": "error", "tx_count": "",
                                "first_tx": "", "wake_ts": "", "gap_days": "",
                                "first_deploy": first_deploy, "funder": "", "fund_wei": "",
                                "fund_ts": "", "enriched_at": int(time.time())}
            if consecutive >= 5:  # the service is probably blocking us - stop and report
                break
    return stats
