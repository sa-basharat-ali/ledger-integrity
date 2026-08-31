#!/usr/bin/env python3
"""python3 run.py   (stdlib only, no install, <1s)"""
import os, sys, json, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import ledger, reconcile as R

line = lambda c="=", n=84: print(c * n)
money = lambda x: f"${x:,.0f}"
d = ledger.build()
truth = d["truth"]
vol = sum(r["amount"] for r in d["ledger"] if r["direction"] == "debit")

print()
line()
print("  LEDGER INTEGRITY UNDER AGENT-SPEED TRAFFIC")
line()
print(f"  {len(d['events']):,} intents / {ledger.DAYS} days / {ledger.N_AGENTS} agents   "
      f"{money(vol)} through the ledger")
print(f"  {len(truth)} injected failures worth {money(sum(t['amount'] for t in truth))}, "
      f"plus slow settlements and")
print(f"  legitimate repeat payments as confounders, which are NOT failures")

# ---- 1 ----------------------------------------------------------------------
print("\n\n1. WHY DAILY TOTALS DO NOT WORK, AND IT IS NOT THE THRESHOLD\n")
g = R.gross_vs_net(d, truth)
tg = sum(r["gross"] for r in g)
tn = sum(abs(r["net"]) for r in g)
print(f"   {'day':<12}{'errors':>8}{'gross error':>14}{'net error':>13}{'hidden by netting':>20}")
print("   " + "-" * 67)
for r in g[:5]:
    print(f"   {r['day']:<12}{r['n']:>8}{money(r['gross']):>14}{money(r['net']):>13}"
          f"{money(r['hidden']):>20}")
print(f"   {'...':<12}")
print("   " + "-" * 67)
print(f"   {'TOTAL':<12}{len(truth):>8}{money(tg):>14}{money(tn):>13}"
      f"{money(tg-tn):>20}")

base = R.daily_totals(d)
print(f"\n   A dropped ledger row makes the processor total look high. A duplicate does the")
print(f"   same. A torn write leaves the debit total correct while the credit leg is gone.")
print(f"   Inside one day those cancel, so the quantity a daily total can see is the NET.")
print(f"   {(tg-tn)/tg:.0%} of the error disappears before anyone compares the two numbers.")
print(f"\n   And when it does fire, which here is {len(base)} of {ledger.DAYS} days, this is the alert:")
print(f"\n       'ledger and processor differ by {money(abs(g[0]['net']))} on {g[0]['day']}'")
print(f"\n   {len(d['events'])//ledger.DAYS:,} intents that day, {money(vol/ledger.DAYS)} of volume, and no")
print(f"   indication which ones. It is not a false alarm and it is not actionable either.")

# ---- 2 ----------------------------------------------------------------------
pi = R.per_intent(d)
s = R.score(pi, truth, d, "per-intent")
print("\n\n2. HALF OF THESE CHECKS DO NOT NEED TO WAIT\n")
print(f"   {'failure mode':<20}{'n':>5}{'caught':>8}{'kind':>13}{'median latency':>17}")
print("   " + "-" * 63)
KIND = {"torn_write": "structural", "out_of_order": "structural",
        "duplicate_capture": "structural", "dropped_ledger": "temporal",
        "retry_storm": "undetectable"}
for r in s["by_mode"]:
    lat = f"{r['median_latency_h']:.2f} h" if r["median_latency_h"] is not None else "never"
    print(f"   {r['mode']:<20}{r['n']:>5}{r['caught']:>8}{KIND[r['mode']]:>13}{lat:>17}")
print(f"\n   A debit with no matching credit is wrong the instant it is written, and so is a")
print(f"   ledger row timestamped before the event that caused it. Both are contradictions")
print(f"   inside data you already hold. No window, no tolerance, no batch, zero latency.")
print(f"\n   The duplicate is structural too, but its clock starts when the second settlement")
print(f"   lands rather than when you look, so the 1.65 hours above is the rail's latency")
print(f"   and not the detector's. Worth separating: one number you can drive to zero, one")
print(f"   you cannot.")
print(f"\n   A missing ledger row is different. It is indistinguishable from one still in")
print(f"   flight until the pending window expires, so its latency is the window by")
print(f"   definition, and the window trades directly against false alarms on slow")
print(f"   settlements. Most reconciliation systems run everything nightly, which imposes")
print(f"   a temporal design on three checks that are structural.")

# ---- 3 ----------------------------------------------------------------------
print("\n\n3. THE ONE THAT CANNOT BE CAUGHT STRUCTURALLY IS THE ONE THAT COSTS\n")
print(f"   {'failure mode':<20}{'caught':>9}{'undetected dollar-hours':>27}{'share':>9}")
print("   " + "-" * 66)
tot_dh = s["dollar_hours"]
for r in sorted(s["by_mode"], key=lambda x: -x["dollar_hours"]):
    frac = "{}/{}".format(r["caught"], r["n"])
    share = r["dollar_hours"] / tot_dh
    print("   {:<20}{:>9}{:>27}{:>9.0%}".format(r["mode"], frac, money(r["dollar_hours"]), share))
print("   " + "-" * 66)
print("   {:<20}{:>9}{:>27}".format("TOTAL", "{}/{}".format(s["caught"], s["total"]), money(tot_dh)))
rs = next(r for r in s["by_mode"] if r["mode"] == "retry_storm")
print(f"\n   Dollar-hours is amount at stake multiplied by hours undetected. A $12 error")
print(f"   caught in an hour and a $40,000 error caught in a day are not the same incident,")
print(f"   and a metric that counts them equally sends you to fix the wrong one.")
print(f"\n   The retry storm is {rs['dollar_hours']/tot_dh:.0%} of it. An agent framework timed out, regenerated")
print(f"   its intent id AND its idempotency key, and paid again. Nothing links the two")
print(f"   records: different id, different key, same money. Every structural check passes")
print(f"   on both rows because both rows are internally perfect.")

# ---- 4 ----------------------------------------------------------------------
print("\n\n4. CATCHING IT MEANS ACCUSING REAL CUSTOMERS, AND HERE IS THE RATE\n")
print(f"   Match on the only thing the two share: same agent, same amount, close in time.")
print(f"   An agent that legitimately pays the same amount twice inside that window is")
print(f"   indistinguishable from a retry, by construction.\n")
print(f"   {'window':>10}{'retry storms caught':>22}{'false accusations':>20}{'precision':>12}")
print("   " + "-" * 64)
sweep = []
for w in (2, 5, 10, 20, 60):
    fz = R.fuzzy_duplicates(d, window_min=w)
    s2 = R.score(pi + fz, truth, d, f"fuzzy {w}m")
    r2 = next(x for x in s2["by_mode"] if x["mode"] == "retry_storm")
    added_fp = s2["false_positives"] - s["false_positives"]
    sweep.append({"window_min": w, "caught": r2["caught"], "added_fp": added_fp,
                  "precision": s2["precision"], "dollar_hours": s2["dollar_hours"]})
    caught_s = "{} of {}".format(r2["caught"], r2["n"])
    print("   {:>10}{:>22}{:>20}{:>12.0%}".format(str(w) + " min", caught_s, added_fp, s2["precision"]))
best = sweep[2]
print(f"\n   Ten minutes catches all of them and wrongly flags {best['added_fp']} legitimate repeat")
print(f"   payments. Sixty minutes catches the same number and flags "
      f"{sweep[-1]['added_fp']}. There is no")
print(f"   setting that catches every retry and accuses nobody, because the two are the")
print(f"   same event with different intent behind it, and intent is not in the data.")

# ---- 5 ----------------------------------------------------------------------
print("\n\n5. THE OTHER KNOB, AND WHY IT IS NOT FREE EITHER\n")
print(f"   {'pending window':>16}{'drops caught':>15}{'false alarms':>15}{'median latency':>17}")
print("   " + "-" * 63)
pend = []
for h in (1, 3, 6, 12, 24):
    p2 = R.per_intent(d, pending_hours=h)
    s3 = R.score(p2, truth, d, f"pending {h}h")
    r3 = next(x for x in s3["by_mode"] if x["mode"] == "dropped_ledger")
    pend.append({"hours": h, "caught": r3["caught"], "fp": s3["false_positives"],
                 "latency": r3["median_latency_h"], "dollar_hours": s3["dollar_hours"]})
    caught_s = "{} of {}".format(r3["caught"], r3["n"])
    print("   {:>16}{:>15}{:>15}{:>14.1f} h".format(
        str(h) + " h", caught_s, s3["false_positives"], r3["median_latency_h"]))
print(f"\n   Every hour you add to the pending window is an hour of undetected exposure on")
print(f"   the drops, bought in exchange for fewer false alarms on the slow settlements.")
print(f"   At one hour you catch everything immediately and page constantly. At 24 hours")
print(f"   you are back to a nightly batch.")
print(f"\n   This is a business decision rather than an engineering one, and the only useful")
print(f"   thing an engineer can do is put the exchange rate in front of whoever owns it.")

json.dump({"gross": tg, "net_visible": tn, "hidden_share": (tg-tn)/tg,
           "daily_alerts": len(base), "per_intent": s,
           "fuzzy_sweep": sweep, "pending_sweep": pend},
          open("results.json", "w"), indent=2, default=str)
print("\n   -> results.json\n")
line()
