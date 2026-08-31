# Ledger integrity under agent-speed traffic

```
python3 run.py      # stdlib only, no install, ~10 seconds
```

23,014 payment intents, 14 days, 400 agents, $5.5M through the ledger. 300 injected
failures with ground truth, plus two confounders that are **not** failures. Deterministic
seed, no real transactions.

---

## The setup

An agentic payments platform holds three records of the same money:

| | |
|---|---|
| **events** | what the platform emitted. An agent asked to pay |
| **ledger** | the internal double-entry record. Every intent should produce a debit and a matching credit summing to zero |
| **processor** | what the external rail actually settled. The closest thing to truth, and it arrives late and out of order |

Reconciliation proves those three still agree. Almost all of it is built for humans, who
transact slowly, retry rarely, and tolerate a next-morning answer.

Agents break all three assumptions. They retry in milliseconds, so one intent becomes
twenty attempts. They read a balance and act on it immediately, so a 300ms stale read is an
overdraft. They run continuously, so a nightly batch means a discrepancy lives for up to 24
hours.

## Four findings

### 1. Daily totals fail for a reason that has nothing to do with the threshold

A dropped ledger row makes the processor total look high. A duplicate does the same. A torn
write leaves the debit total correct while the credit leg is gone. Inside one day those
cancel, so the quantity a daily total can see is the **net**, while the quantity that is
actually wrong is the **gross**.

| | |
|---|---|
| gross error | $71,570 |
| net error, visible to a daily total | $38,838 |
| **hidden by netting** | **$32,732 (46%)** |

And when it does fire, which here is 13 of 14 days, the alert is *"ledger and processor
differ by $4,437 on 2026-08-01."* That day had 1,643 intents and $392,933 of volume, with
no indication which ones. Not a false alarm, and not actionable either.

### 2. Half of these checks do not need to wait

| mode | kind | median latency |
|---|---|---|
| torn_write | structural | **0.00 h** |
| out_of_order | structural | **0.00 h** |
| duplicate_capture | structural | 1.65 h (the rail's latency, not the detector's) |
| dropped_ledger | temporal | 6.00 h (= the pending window, by definition) |
| retry_storm | undetectable structurally | never |

A debit with no matching credit is wrong the instant it is written, and so is a ledger row
timestamped before the event that caused it. Both are contradictions inside data you
already hold: no window, no tolerance, no batch.

A missing ledger row is different. It is indistinguishable from one still in flight until
the pending window expires. Most reconciliation systems run everything nightly, which
imposes a temporal design on checks that are structural.

### 3. The failure you cannot catch structurally is 93% of the cost

Measured in **undetected dollar-hours**: amount at stake times hours unnoticed. A $12 error
caught in an hour and a $40,000 error caught in a day are not the same incident, and a
metric that counts them equally sends you to fix the wrong one.

| mode | caught | dollar-hours | share |
|---|---|---|---|
| **retry_storm** | **0/53** | **$1,661,153** | **93%** |
| dropped_ledger | 62/62 | $103,837 | 6% |
| duplicate_capture | 64/64 | $24,389 | 1% |
| out_of_order | 54/54 | $0 | 0% |
| torn_write | 67/67 | $0 | 0% |

The retry storm: an agent framework timed out, regenerated its intent id **and** its
idempotency key, and paid again. Nothing links the two records. Different id, different
key, same money. Every structural check passes on both rows because both rows are
internally perfect.

### 4. Catching it means accusing real customers, and here is the exchange rate

Match on the only thing the two share: same agent, same amount, close in time. An agent
that legitimately pays the same amount twice inside that window is indistinguishable from a
retry, by construction.

| window | retry storms caught | legitimate payments wrongly flagged |
|---|---|---|
| 2 min | 17 of 53 | 8 |
| 5 min | 41 of 53 | 31 |
| **10 min** | **53 of 53** | **61** |
| 60 min | 53 of 53 | 320 |

There is no setting that catches every retry and accuses nobody, because the two are the
same event with different intent behind them, and intent is not in the data.

The pending window has the same shape. At 1 hour you catch drops immediately and raise
17,341 false alarms on slow settlements. At 24 hours you raise 64 and you are back to a
nightly batch. Six hours gives 233.

Both are business decisions, not engineering ones. The useful thing an engineer can do is
put the exchange rate in front of whoever owns the decision.

## What this deliberately does not claim

The confounders are the point. **Slow settlement is not a failure** and is injected
separately, because treating it as one is how a reconciliation system earns a reputation
for crying wolf. **Legitimate repeat payments are not failures** either, and exist so the
fuzzy matcher has something real to get wrong.

An earlier version of this had the detector catch 298 of 298 with zero false positives.
That was not a result, it was the detector inverting the injector. Adding the retry storm
and the two confounders is what made it honest, and the honest version catches 247 of 300
and misses the expensive one.

## Files

| | |
|---|---|
| `ledger.py` | the three systems, the five failure modes, the two confounders |
| `reconcile.py` | daily-totals baseline, per-intent structural and temporal checks, fuzzy duplicate matching, scoring |
| `run.py` | the report |

## Where this comes from

I am the sole data engineer for a smart-retail platform, and the largest thing I have found
there was an 80% upstream data loss: 37,000 rows landing against 177,000 expected,
root-caused to edge store corruption plus 31 branches that had been silently dead for
weeks while reporting healthy. I shipped a six-fix package on a four-stage rollout, then
built the ETL observability that would have caught it, which caught two more real issues on
its first production run.

Before that I built fraud detection and merchant risk models at Geidea, Saudi Arabia's
largest fintech: 40,000+ merchants, 400,000+ POS terminals, over a million transactions a
day.

The reason this project is about reconciliation specifically is that the retail version and
the payments version are the same problem. Something that should have landed did not, every
dashboard stayed green, and the gap between those two facts is measured in hours nobody was
counting.

Syed Ahmed Basharat Ali. sabasharat.ali@gmail.com | basharat.net
