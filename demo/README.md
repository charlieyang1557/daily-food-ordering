# Demo script — Daily Food Ordering

A turnkey walkthrough that exercises **every SKILL.md function**. The mock
provider drives the decision paths *deterministically* (no browser flakiness);
the real DoorDash provider shows live menu retrieval + stop-before-pay.

Run all of these from the skill directory:

```bash
cd ~/.openclaw/workspace/skills/daily-food-ordering   # or the repo root
```

Every result includes `"charged": false` — **no payment is ever completed.**

| # | Command | Shows (SKILL.md function) | Result |
|---|---------|---------------------------|--------|
| 1 | `python3 run.py --config demo/charlie-day-one.yaml` | **CONFIRM** band — day one, `auto_approve_under: 0` confirms everything (the trust dial starts low) | `CONFIRM above_auto_approve`, places nothing |
| 2 | `python3 run.py --config demo/charlie-trusted.yaml` | **AUTO** — trust granted (`auto_approve_under: 18`), routine order goes through | `AUTO within_auto_approve`, **placed** |
| 3 | `python3 run.py --config demo/charlie-no-fallback.yaml --scenario over_budget` | **BLOCK over-budget** — refuses on price, places nothing | `BLOCK over_daily_max` |
| 4 | `python3 run.py --config demo/charlie-trusted.yaml --scenario allergen` | **Safety BLOCK → fallback** — refuses the peanut dish, settles on the safe Chipotle fallback | `CONFIRM fallback_in_use`, selected **Chipotle** |
| 5 | `python3 run.py --config demo/charlie-no-fallback.yaml --scenario allergen` | **Hard refusal (P0)** — unsafe option + no safe fallback → block + loud notify | `BLOCK allergy_violation` |
| 6 | `python3 run.py --config demo/invalid.yaml` | **Fail-loud config validation** — bad allergen rejected, places nothing | exit `2`, `{"error":"config_invalid","detail":"unsupported allergen: tuna"}` |
| 7 | `python3 run.py --config demo/charlie-trusted.yaml --claim-slot` (run twice) | **Idempotency** — second run the same day no-ops | run 1 placed; run 2 `already_ran: true` |
| 8 | `python3 run.py --provider doordash --query thai --config demo/charlie-unrestricted.yaml` | **Real DoorDash** — live menu → AUTO → add to cart → **STOP before payment** | `STOPPED_BEFORE_PAYMENT` (or fails closed), `charged:false` |

### The trust-dial story (run 1 then 2 back to back)
Day one it **asks** (`CONFIRM`); after you raise `auto_approve_under`, the same
order **just happens** (`AUTO`). Autonomy is earned, not assumed.

### The safety story (run 4 then 5)
With a fallback, an unsafe pick is **rescued** to your pre-vetted safe choice
(`CONFIRM fallback_in_use`). Without one, it **refuses** (`BLOCK`) — it never
relaxes an allergy to fill the order.

## Real DoorDash (one-time setup)

```bash
python3 run.py --provider doordash --login   # headed: pass the human check, sign in, set address, press Enter
python3 run.py --provider doordash --query thai --config demo/charlie-unrestricted.yaml
```

In **one** browser session, the adapter retrieves a real menu, **searches the
store for the engine's chosen dish**, adds it (auto-completing *every* required
customization group — protein, spice, etc.), goes straight to the real checkout
in-session, and **hard-stops at the "Place Order" button** — returning
`STOPPED_BEFORE_PAYMENT` with the real total + a screenshot. It never clicks pay.
If it can't reach the pay gate it returns `FAILED` (never a fake success).

Verified live result:
```
STATUS: STOPPED_BEFORE_PAYMENT | placed: false (carted, NOT paid) | charged: false
# placed=false is honest: stopping before pay is not a placed order, so a
# scheduled --claim-slot run does NOT burn the day on a stop-before-pay.
carted: <dish>  | checkout_total_usd: <real total> | screenshot: ~/.daily-food-ordering/screenshots/<key>.png
```

Notes:
- **Start with an empty DoorDash cart** for an accurate total — the adapter adds
  one item; it does not clear pre-existing cart items, so leftovers inflate the
  total (still reconciled against `daily_max`, failing closed if over).
- The adapter finds the approved dish via the store **search box** and
  auto-completes its required customization (any number of "select 1" groups).
  Only if it genuinely can't add that dish does it **substitute the cheapest
  item it can add**, reporting `summary.substituted_for` — honest either way.
- Use the *unrestricted* config — with restrictions, real DoorDash items are
  `verified_safe=false` and the engine correctly BLOCKs them (safety-first).

## Operating it through Discord

The skill runs **inside OpenClaw**, which owns the `message`→Discord tool (Claude
Code can't post to Discord directly). To demo end-to-end on Discord:

1. Ensure the skill is installed: `bash install.sh`
2. Enable the daily cron job (it ships **disabled**): set `"enabled": true` on the
   `daily-food-ordering` job in `~/.openclaw/cron/jobs.json`, **or** trigger the
   skill manually from OpenClaw.
3. OpenClaw runs `python3 run.py --provider doordash --claim-slot …`, then posts a
   severity-calibrated summary to Discord channel `<your-discord-channel-id>` via the
   `message` tool — saying plainly that the order was **carted and stopped before
   payment, no charge made**.

The cron job deliberately does **not** fall back to the mock provider and never
passes `--complete-payment`, so a scheduled run can never fake a success or charge.

**Self-contained alternative (no OpenClaw agent):** set a Discord webhook in the
environment and add `--notify` — `run.py` posts the severity-calibrated summary
to Discord itself (AUTO/CONFIRM/BLOCK/stopped-before-pay):

```bash
export DFO_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"   # secret — never commit
python3 run.py --provider doordash --claim-slot --notify
```
