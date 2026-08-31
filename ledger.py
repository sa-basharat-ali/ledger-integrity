"""Three systems that are supposed to agree, and the ways they quietly stop agreeing.

An agentic payments platform has at least three records of the same money:

  events     what the platform emitted. An agent asked to pay, and the intent was logged
  ledger     the internal double-entry record. Every intent should produce a debit and a
             matching credit that sum to zero
  processor  what the external rail actually settled. This is the closest thing to truth
             and it arrives late and out of order

Reconciliation is the practice of proving those three still agree. Most of it is built for
humans, who transact slowly, retry rarely, and tolerate a next-day answer.

Agents break all three assumptions:

  they retry in milliseconds, not minutes, so one intent becomes twenty attempts
  they read a balance and act on it immediately, so a 300ms stale read is an overdraft
  they run continuously, so a nightly batch means a discrepancy lives for up to 24 hours

The five failure modes below are what that looks like in the data. Only the first is
visible to a check that compares daily totals, and even that one only when it does not
happen to be cancelled out by another.

Deterministic seed. No real transactions.
"""
import random, hashlib
from datetime import datetime, timedelta

SEED = 90210
DAYS = 14
N_AGENTS = 400
START = datetime(2026, 8, 1)

# Real failures. late_settlement is deliberately NOT here: a slow settlement is correct
# behaviour, not an error, and treating it as one is how a reconciliation system earns its
# reputation for crying wolf. It is injected separately as a confounder.
MODES = ("dropped_ledger", "duplicate_capture", "retry_storm", "torn_write", "out_of_order")


def _amount(rnd):
    # B2B agent payments: frequent and mid-sized, with a long tail of large settlements
    return round(min(max(rnd.lognormvariate(4.5, 1.4), 1.0), 90000.0), 2)


def build(seed=SEED, failure_rate=0.012):
    rnd = random.Random(seed)
    agents = [f"agt_{i:04d}" for i in range(N_AGENTS)]

    events, ledger, processor, truth = [], [], [], []
    intent_n = 0

    for day in range(DAYS):
        base = START + timedelta(days=day)
        # agents do not sleep, but their principals do: volume dips overnight
        for _ in range(rnd.randint(1300, 1900)):
            intent_n += 1
            iid = f"int_{intent_n:07d}"
            agent = rnd.choice(agents)
            amt = _amount(rnd)
            hour = min(int(abs(rnd.gauss(13, 5))), 23)
            ts = base + timedelta(hours=hour, minutes=rnd.randrange(60),
                                  seconds=rnd.randrange(60))
            # idempotency key. Real agent frameworks regenerate this on retry more often
            # than anyone admits, which is why key matching alone does not catch duplicates.
            idem = hashlib.sha1(f"{iid}|{agent}|{amt}".encode()).hexdigest()[:16]

            mode = None
            if rnd.random() < failure_rate:
                mode = rnd.choice(MODES)
            # Confounders, not failures. Both exist to make the easy version of this
            # problem impossible: a slow settlement looks exactly like a lost one until it
            # arrives, and an agent legitimately paying the same amount twice in an hour
            # looks exactly like a retry.
            slow = mode is None and rnd.random() < 0.010
            legit_repeat = mode is None and rnd.random() < 0.014

            events.append({"intent_id": iid, "agent_id": agent, "amount": amt,
                           "ts": ts.isoformat(timespec="seconds"), "idem_key": idem})

            # ---- ledger: double entry, debit the agent wallet, credit the merchant ----
            led_ts = ts + timedelta(milliseconds=rnd.randrange(20, 900))
            if mode == "out_of_order":
                led_ts = ts - timedelta(seconds=rnd.randrange(2, 45))   # lands "before" it happened
            if mode != "dropped_ledger":
                ledger.append({"intent_id": iid, "account": agent, "direction": "debit",
                               "amount": amt, "ts": led_ts.isoformat(timespec="seconds")})
                if mode != "torn_write":                                 # credit leg goes missing
                    ledger.append({"intent_id": iid, "account": "merchant_pool",
                                   "direction": "credit", "amount": amt,
                                   "ts": led_ts.isoformat(timespec="seconds")})

            # ---- processor: external settlement, always late, sometimes very late ----
            lag_min = rnd.randrange(5, 240)
            if slow:
                lag_min = rnd.randrange(6 * 60, 30 * 60)
            settled = ts + timedelta(minutes=lag_min)
            processor.append({"intent_id": iid, "amount": amt,
                              "settled_ts": settled.isoformat(timespec="seconds")})
            if mode == "duplicate_capture":
                # Same intent_id settled twice. Structurally obvious once you look per
                # intent instead of per day.
                processor.append({"intent_id": iid, "amount": amt,
                                  "settled_ts": (settled + timedelta(
                                      milliseconds=rnd.randrange(150, 2500))
                                  ).isoformat(timespec="seconds")})

            if mode == "retry_storm":
                # The expensive one. The agent framework timed out, regenerated the intent
                # id AND the idempotency key, and paid again. Nothing links the two records.
                # It is a duplicate that is indistinguishable, by construction, from a
                # customer who legitimately paid the same amount twice.
                intent_n += 1
                dup = f"int_{intent_n:07d}"
                d_ts = ts + timedelta(seconds=rnd.randrange(1, 400))
                events.append({"intent_id": dup, "agent_id": agent, "amount": amt,
                               "ts": d_ts.isoformat(timespec="seconds"),
                               "idem_key": hashlib.sha1(f"{dup}|{agent}|{amt}".encode()
                                                        ).hexdigest()[:16]})
                d_led = d_ts + timedelta(milliseconds=rnd.randrange(20, 900))
                ledger.append({"intent_id": dup, "account": agent, "direction": "debit",
                               "amount": amt, "ts": d_led.isoformat(timespec="seconds")})
                ledger.append({"intent_id": dup, "account": "merchant_pool",
                               "direction": "credit", "amount": amt,
                               "ts": d_led.isoformat(timespec="seconds")})
                processor.append({"intent_id": dup, "amount": amt,
                                  "settled_ts": (d_ts + timedelta(
                                      minutes=rnd.randrange(5, 240))).isoformat(timespec="seconds")})
                truth.append({"intent_id": dup, "mode": "retry_storm", "amount": amt,
                              "ts": d_ts.isoformat(timespec="seconds")})

            if legit_repeat:
                # A genuine second payment: same agent, same amount, minutes apart. Not an
                # error. Present so that any fuzzy duplicate detector has something real to
                # get wrong.
                intent_n += 1
                rep = f"int_{intent_n:07d}"
                r_ts = ts + timedelta(seconds=rnd.randrange(60, 3000))
                events.append({"intent_id": rep, "agent_id": agent, "amount": amt,
                               "ts": r_ts.isoformat(timespec="seconds"),
                               "idem_key": hashlib.sha1(f"{rep}|{agent}|{amt}".encode()
                                                        ).hexdigest()[:16]})
                r_led = r_ts + timedelta(milliseconds=rnd.randrange(20, 900))
                ledger.append({"intent_id": rep, "account": agent, "direction": "debit",
                               "amount": amt, "ts": r_led.isoformat(timespec="seconds")})
                ledger.append({"intent_id": rep, "account": "merchant_pool",
                               "direction": "credit", "amount": amt,
                               "ts": r_led.isoformat(timespec="seconds")})
                processor.append({"intent_id": rep, "amount": amt,
                                  "settled_ts": (r_ts + timedelta(
                                      minutes=rnd.randrange(5, 240))).isoformat(timespec="seconds")})

            if mode and mode != "retry_storm":
                truth.append({"intent_id": iid, "mode": mode, "amount": amt,
                              "ts": ts.isoformat(timespec="seconds")})

    processor.sort(key=lambda r: r["settled_ts"])
    return {"events": events, "ledger": ledger, "processor": processor, "truth": truth}


if __name__ == "__main__":
    d = build()
    from collections import Counter
    print(f"{len(d['events']):,} intents over {DAYS} days, {N_AGENTS} agents")
    print(f"{len(d['ledger']):,} ledger rows, {len(d['processor']):,} processor records")
    print(f"{len(d['truth'])} injected failures: {dict(Counter(t['mode'] for t in d['truth']))}")
    exposure = sum(t["amount"] for t in d["truth"])
    print(f"${exposure:,.0f} of intent value touched by a failure")
