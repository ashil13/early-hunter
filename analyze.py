"""
Early Hunter - candidate builder

Turns raw deployments + wallet history into explainable "Candidates".
A candidate = one wallet, or several wallets linked by funding evidence.
Every point of the score comes from a named signal with the evidence behind it.
Nothing here claims that wallets belong to the same person - links are
probabilistic and based only on observable on-chain facts.
"""

from collections import defaultdict

DAY = 86400
MIN_SCORE = 15
MAX_CANDIDATES = 60


def short(a):
    return a[:8] + "…" + a[-6:] if a and len(a) > 16 else a


def eth(wei):
    try:
        return f"{int(wei) / 1e18:.4f}"
    except Exception:
        return "?"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def num(row, key):
    try:
        return int(row.get(key) or 0) or None
    except Exception:
        return None


def build_candidates(deployments, wallets, funders, seen, now):
    """deployments: list of (contract, creator, block, ts, tx). Returns (candidates, seen)."""
    by_creator = defaultdict(list)
    for contract, creator, block, ts, tx in deployments:
        by_creator[creator].append((contract, ts, tx))
    deployers = {c for c, v in by_creator.items() if len(v) >= 2}

    # ---------- links between wallets (funding evidence)
    uf = UnionFind()
    for d in deployers:
        uf.find(d)
    funded_by = defaultdict(list)
    for d in deployers:
        f = (wallets.get(d) or {}).get("funder")
        if f:
            funded_by[f].append(d)

    links = []      # funding relationships we may show
    link_kind = {}  # funder -> "shared" | "direct" | "service"
    for f, members in funded_by.items():
        info = funders.get(f) or {}
        is_contract = info.get("is_contract") == "1"
        is_labeled = bool((info.get("name") or "").strip())  # exchanges, bridges, known services
        if len(members) > 15 or is_contract or is_labeled:
            link_kind[f] = "service"           # exchange / bridge / airdrop style: not evidence
        elif f in deployers:
            link_kind[f] = "direct"
            for m in members:
                uf.union(m, f)
        elif len(members) >= 2:
            link_kind[f] = "shared"
            for m in members[1:]:
                uf.union(members[0], m)
        else:
            link_kind[f] = "single"

    groups = defaultdict(list)
    for d in deployers:
        groups[uf.find(d)].append(d)

    candidates = []
    for members in groups.values():
        members.sort()
        member_set = set(members)
        contracts = sorted(
            [(c, creator, ts, tx) for creator in members for (c, ts, tx) in by_creator[creator]],
            key=lambda x: x[2],
        )
        n = len(contracts)
        t_first, t_last = contracts[0][2], contracts[-1][2]
        signals = []

        def add(key, label, points, detail):
            signals.append({"key": key, "label": label, "points": points, "detail": detail})

        # ---- fresh wallet
        best = None
        for w in members:
            r = wallets.get(w) or {}
            first_tx, first_dep = num(r, "first_tx"), num(r, "first_deploy")
            if r.get("status") == "ok" and first_tx and first_dep:
                age = max(0, first_dep - first_tx) / DAY
                pts = 20 if age <= 1 else 17 if age <= 3 else 13 if age <= 7 else 7 if age <= 30 else 0
                if pts and (best is None or pts > best[0]):
                    best = (pts, w, age)
        if best:
            add("fresh_wallet", "Fresh wallet", best[0],
                f"{short(best[1])} made its first transaction only {best[2]:.1f} days before its first contract deployment.")

        # ---- dormant then active
        best = None
        for w in members:
            r = wallets.get(w) or {}
            gap = num(r, "gap_days")
            if gap:
                pts = 20 if gap >= 180 else 17 if gap >= 90 else 14
                if best is None or pts > best[0]:
                    best = (pts, w, gap)
        if best:
            add("dormant_burst", "Woke up after long silence", best[0],
                f"{short(best[1])} had been inactive for {best[2]} days before this activity started.")

        # ---- several contracts
        pts = 18 if n >= 10 else 15 if n >= 6 else 12 if n >= 4 else 8
        add("multi_contract", "Several contracts", pts,
            f"{plural(n, 'contract')} deployed by {plural(len(members), 'wallet')} in the observed window.")

        # ---- burst
        times = [c[2] for c in contracts]
        best_burst = 0
        j = 0
        for i in range(len(times)):
            while times[i] - times[j] > 2 * DAY:
                j += 1
            best_burst = max(best_burst, i - j + 1)
        if best_burst >= 3:
            add("burst", "Deployment burst", 10, f"{best_burst} contracts were deployed within 48 hours.")

        # ---- several deploying wallets
        if len(members) >= 2:
            add("multi_wallet", "Several related wallets", 14 if len(members) >= 4 else 10,
                f"{len(members)} wallets deploying contracts are linked by funding evidence.")

        # ---- coordinated funding
        cluster_links = []
        for w in members:
            r = wallets.get(w) or {}
            f = r.get("funder")
            if not f:
                continue
            cluster_links.append({"from": f, "to": w, "eth": eth(r.get("fund_wei")),
                                  "time": num(r, "fund_ts"), "kind": link_kind.get(f, "single")})
        shared = {}
        for l in cluster_links:
            if l["kind"] in ("shared", "direct"):
                shared.setdefault(l["from"], []).append(l)
        if shared:
            biggest = max(shared.values(), key=len)
            size = len(biggest)
            pts = 15 if size <= 6 else 8
            ftimes = [l["time"] for l in biggest if l["time"]]
            detail = f"{plural(size, 'wallet')} received their first funds from {short(biggest[0]['from'])}."
            if len(ftimes) >= 2 and max(ftimes) - min(ftimes) <= DAY:
                pts += 5
                detail += " The funding happened within 24 hours."
            add("coordinated_funding", "Coordinated funding", pts, detail)

        # ---- mass deployment penalty (raw activity is not an opportunity)
        if n >= 30:
            add("automation", "Looks like automation", -25,
                f"{n} contracts is typical of bots or contract factories, not a team building a protocol.")

        score = max(0, min(100, sum(s["points"] for s in signals)))
        if score < MIN_SCORE:
            continue

        # ---- confidence = how complete our evidence is (not the chance of a real project)
        analysed = sum(1 for w in members if (wallets.get(w) or {}).get("status") == "ok")
        kinds = len({s["key"] for s in signals if s["points"] > 0})
        confidence = 25 + 10 * min(kinds, 5) + (15 if analysed == len(members) else 0)
        if analysed == 0:
            confidence = min(confidence, 30)
        confidence = min(confidence, 95)

        cid = "C-" + members[0][2:8].upper()
        first_seen = seen.get(cid) or now
        seen[cid] = first_seen

        # ---- plain-language explanation (only from evidence above)
        parts = [s["detail"] for s in signals if s["points"] > 0]
        why = "This candidate was flagged because " + " ".join(
            (p[0].lower() + p[1:]) if i == 0 else p for i, p in enumerate(parts)
        ) if parts else "Little evidence so far."
        why += " No project identity has been established yet."

        # ---- timeline
        events = []
        for w in members:
            r = wallets.get(w) or {}
            if num(r, "first_tx"):
                events.append((num(r, "first_tx"), "wallet", f"Wallet {short(w)} first seen on-chain"))
            if num(r, "wake_ts") and num(r, "gap_days"):
                events.append((num(r, "wake_ts"), "wake", f"Wallet {short(w)} became active again after {r['gap_days']} days"))
            if num(r, "fund_ts"):
                events.append((num(r, "fund_ts"), "funding", f"{short(w)} received {eth(r.get('fund_wei'))} ETH from {short(r['funder'])}"))
        grouped = defaultdict(list)
        for c, creator, ts, tx in contracts:
            grouped[(creator, ts // 3600)].append(ts)
        for (creator, _), tss in grouped.items():
            events.append((min(tss), "deploy", f"{short(creator)} deployed {plural(len(tss), 'contract')}"))
        events.sort()
        timeline = [{"time": t, "kind": k, "text": x} for t, k, x in events[:60]]

        wallet_info = []
        for w in members:
            r = wallets.get(w) or {}
            first_tx = num(r, "first_tx")
            wallet_info.append({
                "address": w, "contracts": len(by_creator[w]),
                "status": r.get("status", "pending"),
                "age_days": round((now - first_tx) / DAY, 1) if first_tx else None,
                "tx_count": num(r, "tx_count"),
            })

        candidates.append({
            "id": cid, "score": score, "confidence": confidence,
            "stage": "Unclassified early activity", "category": "Unknown (not analysed yet)",
            "first_detected": first_seen, "last_activity": t_last, "first_activity": t_first,
            "contracts_total": n,
            "contracts_24h": sum(1 for c in contracts if now - c[2] <= DAY),
            "signals": signals, "why": why,
            "wallets": wallet_info, "funding": cluster_links, "timeline": timeline,
            "contracts": [{"address": c, "creator": cr, "time": ts, "tx": tx}
                          for c, cr, ts, tx in reversed(contracts[-40:])],
            "pending": analysed < len(members),
        })

    candidates.sort(key=lambda c: (-c["score"], -c["last_activity"]))
    return candidates[:MAX_CANDIDATES], seen
