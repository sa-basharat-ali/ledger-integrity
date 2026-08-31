"""Two reconcilers over the same three systems.

  daily_totals   sum the ledger, sum the processor, compare, once a day. This is what
                 almost everybody runs, because it is what you build when your customers
                 are humans and a next-morning answer is fine

  per_intent     match every intent across all three systems, check double-entry balance
                 per intent, and distinguish "has not settled yet" from "is never going
                 to settle"

The interesting part is not that the second one finds more. It is WHY the first one finds
so little, which is a property of summing rather than a property of the threshold.
"""
from collections import defaultdict
from datetime import datetime, timedelta
import statistics as st

TOL_PCT = 0.005          # daily totals within 0.5% is "reconciled" at most shops
PENDING_HOURS = 6        # how long an unsettled intent is given before it counts as missing


def _dt(s):
    return datetime.fromisoformat(s)


# ------------------------------------------------------------------- baseline
def daily_totals(data, tol_pct=TOL_PCT):
    """Compare daily sums. Detection, when it happens, lands at the next batch run,
    which we take as midnight after the day being checked."""
    led = defaultdict(float)
    proc = defaultdict(float)
    for r in data["ledger"]:
        if r["direction"] == "debit":
            led[r["ts"][:10]] += r["amount"]
    for r in data["processor"]:
        proc[r["settled_ts"][:10]] += r["amount"]

    findings = []
    for day in sorted(set(led) | set(proc)):
        l, p = led.get(day, 0.0), proc.get(day, 0.0)
        base = max(l, p, 1.0)
        net = p - l
        if abs(net) / base > tol_pct:
            detected = _dt(day + "T00:00:00") + timedelta(days=1)
            findings.append({"day": day, "net_diff": net, "detected_at": detected,
                             "scope": "whole day, no intent identified"})
    return findings


def gross_vs_net(data, truth):
    """Why the baseline misses so much, quantified.

    A dropped ledger row makes the processor total look too high. A duplicate capture
    makes it look too high as well, but a torn write makes the debit total look right
    while the credit leg is gone. Errors of opposite sign inside the same day cancel
    before anyone compares the two numbers, so the quantity a daily total can see is the
    NET, while the quantity that is actually wrong is the GROSS."""
    by_day = defaultdict(lambda: {"gross": 0.0, "net": 0.0, "n": 0})
    for t in truth:
        d = by_day[t["ts"][:10]]
        d["n"] += 1
        d["gross"] += t["amount"]
        # sign as it appears in a processor-minus-ledger comparison
        sign = {"dropped_ledger": +1, "duplicate_capture": +1, "retry_storm": +1,
                "torn_write": 0, "out_of_order": 0}[t["mode"]]
        d["net"] += sign * t["amount"]
    rows = []
    for day in sorted(by_day):
        d = by_day[day]
        rows.append({"day": day, "n": d["n"], "gross": d["gross"], "net": d["net"],
                     "hidden": d["gross"] - abs(d["net"])})
    return rows


# ------------------------------------------------------------------ proposed
def per_intent(data, pending_hours=PENDING_HOURS):
    """Match every intent across all three systems.

    Two classes of check, and the distinction matters more than any threshold:

      STRUCTURAL   a debit with no matching credit is wrong the instant it is written.
                   No waiting, no window, no tolerance. Detection latency is zero
      TEMPORAL     a missing ledger row is indistinguishable from an in-flight one until
                   the pending window expires. Detection latency is the window, and the
                   window is a direct trade against false alarms on slow settlements

    Almost every reconciliation system treats everything as temporal and runs it nightly.
    Half of these do not need to wait at all."""
    ev = {e["intent_id"]: e for e in data["events"]}
    led = defaultdict(list)
    for r in data["ledger"]:
        led[r["intent_id"]].append(r)
    proc = defaultdict(list)
    for r in data["processor"]:
        proc[r["intent_id"]].append(r)

    findings = []
    for iid, e in ev.items():
        ets = _dt(e["ts"])
        L, P = led.get(iid, []), proc.get(iid, [])

        # --- structural: detectable the moment the rows exist ---
        if L:
            debit = sum(r["amount"] for r in L if r["direction"] == "debit")
            credit = sum(r["amount"] for r in L if r["direction"] == "credit")
            if abs(debit - credit) > 0.005:
                findings.append({"intent_id": iid, "mode": "torn_write",
                                 "amount": abs(debit - credit), "occurred_at": ets,
                                 "detected_at": max(_dt(r["ts"]) for r in L), "kind": "structural"})
            bad_ts = [r for r in L if _dt(r["ts"]) < ets]
            if bad_ts:
                findings.append({"intent_id": iid, "mode": "out_of_order",
                                 "amount": e["amount"], "occurred_at": ets,
                                 "detected_at": max(_dt(r["ts"]) for r in L), "kind": "structural"})
        if len(P) > 1:
            findings.append({"intent_id": iid, "mode": "duplicate_capture",
                             "amount": sum(r["amount"] for r in P[1:]), "occurred_at": ets,
                             "detected_at": _dt(P[1]["settled_ts"]), "kind": "structural"})

        # --- temporal: needs the pending window to distinguish late from lost ---
        deadline = ets + timedelta(hours=pending_hours)
        if not L:
            findings.append({"intent_id": iid, "mode": "dropped_ledger",
                             "amount": e["amount"], "occurred_at": ets,
                             "detected_at": deadline, "kind": "temporal"})
        elif P and _dt(P[0]["settled_ts"]) > deadline:
            # Unsettled past the window. Might be lost, might just be slow. The detector
            # cannot tell, which is exactly the point: this is where the false alarms live.
            findings.append({"intent_id": iid, "mode": "dropped_ledger",
                             "amount": e["amount"], "occurred_at": ets,
                             "detected_at": deadline, "kind": "temporal"})
    return findings


def fuzzy_duplicates(data, window_min=10, findings=None):
    """The hard one. A retry that regenerated its intent id and idempotency key shares
    nothing with the original except agent, amount, and being close in time.

    So match on exactly that, and accept what it costs. An agent that legitimately pays
    the same amount twice inside the window is indistinguishable from a retry, by
    construction, and no amount of cleverness in this function changes that. The window is
    the whole knob: short and it misses real retries, long and it accuses real customers.
    """
    by_key = defaultdict(list)
    for e in data["events"]:
        by_key[(e["agent_id"], round(e["amount"], 2))].append(e)
    out = []
    for (agent, amt), evs in by_key.items():
        if len(evs) < 2:
            continue
        evs.sort(key=lambda e: e["ts"])
        for a, b in zip(evs, evs[1:]):
            gap = (_dt(b["ts"]) - _dt(a["ts"])).total_seconds() / 60
            if gap <= window_min:
                out.append({"intent_id": b["intent_id"], "mode": "retry_storm",
                            "amount": b["amount"], "occurred_at": _dt(b["ts"]),
                            "detected_at": _dt(b["ts"]), "kind": "fuzzy",
                            "gap_min": gap})
    return out


# ----------------------------------------------------------------- evaluation
def score(findings, truth, data, label):
    """Detection rate, latency, and the metric that actually matters.

    UNDETECTED DOLLAR-HOURS is the money-weighted version of time-to-detection: for every
    discrepancy, the amount at stake multiplied by the hours it sat unnoticed. A $12 error
    caught in an hour and a $40,000 error caught in a day are not the same incident, and
    any metric that counts them equally will send you to fix the wrong one."""
    tmap = {t["intent_id"]: t for t in truth}
    end = max(_dt(e["ts"]) for e in data["events"]) + timedelta(hours=24)

    first = {}
    for f in findings:
        k = f["intent_id"]
        if k not in first or f["detected_at"] < first[k]["detected_at"]:
            first[k] = f
    tp = {k: v for k, v in first.items() if k in tmap}
    fp = [v for k, v in first.items() if k not in tmap]

    per_mode = defaultdict(lambda: {"n": 0, "caught": 0, "lat": [], "dh": 0.0})
    for t in truth:
        m = per_mode[t["mode"]]
        m["n"] += 1
        occurred = _dt(t["ts"])
        if t["intent_id"] in tp:
            det = tp[t["intent_id"]]["detected_at"]
            hrs = max((det - occurred).total_seconds() / 3600, 0.0)
            m["caught"] += 1
            m["lat"].append(hrs)
            m["dh"] += t["amount"] * hrs
        else:
            m["dh"] += t["amount"] * ((end - occurred).total_seconds() / 3600)

    rows = [{"mode": k, "n": v["n"], "caught": v["caught"],
             "median_latency_h": st.median(v["lat"]) if v["lat"] else None,
             "dollar_hours": v["dh"]} for k, v in sorted(per_mode.items())]
    return {"policy": label, "by_mode": rows,
            "caught": sum(r["caught"] for r in rows), "total": len(truth),
            "dollar_hours": sum(r["dollar_hours"] for r in rows),
            "false_positives": len(fp),
            "precision": len(tp) / max(len(first), 1)}
