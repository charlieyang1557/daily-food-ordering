---
name: daily-food-ordering
description: >
  Order the user's single daily meal, safely and autonomously. Runs on a daily
  schedule (an OpenClaw cron job) and also when the user says "order food,"
  "place an order," "set up daily food ordering," or asks to change the config.

  It works within a per-meal budget. Orders under the user's auto-approve
  threshold go through automatically; pricier-but-in-budget orders ask for a
  yes; anything over budget or unsafe is refused, not asked about. It never
  spends beyond the user's limits, never acts outside granted authority, and
  never orders food that violates the user's dietary or allergy restrictions.

  For a real order it uses the DoorDash adapter (Playwright, headed): retrieves a
  real menu, drives a real cart to checkout, and HARD-STOPS before payment (never
  charges). The deterministic mock provider is for tests/dry-runs ONLY — never use
  mock to fulfill a real "order my food" request. Reads config from
  user_preferences.yaml. Do NOT use it for anything but the user's own single
  daily meal (no team lunches / group orders).
allowed-tools: ["exec", "message"]
metadata:
  openclaw:
    emoji: "🍱"
    requires:
      bins: ["python3"]
---

# Daily Food Ordering

A deterministic safety engine decides; a provider fetches options and places the
order; the agent only ranks/phrases and relays notifications. The safety, budget,
and final AUTO/CONFIRM/BLOCK call are **code** (`engine/` + `run.py`) and run with
any LLM backend or none. The agent must NOT re-decide budget or safety itself.

Run everything from the skill directory:

    cd ~/.openclaw/workspace/skills/daily-food-ordering

## One-time setup (real DoorDash only)

DoorDash sits behind a human-check wall and shows menus only to a logged-in
session. Warm a persistent browser profile ONCE, by a human:

    [code]  /Users/openclaw/miniconda3/bin/python3 run.py --provider doordash --login

A headed browser opens; the human passes the check, signs in, and sets a
delivery address. The profile persists, so later runs reuse the session. Skip
this entirely for mock/dry-run.

## Instructions

When invoked, run the deterministic pipeline, then resolve its verdict. Steps
marked `[code]` MUST be done by executing the script — do NOT reason about
budgets, restrictions, or the order decision yourself. Steps marked `[agent]`
are yours (ranking is already inside the engine; your job is phrasing + the
human confirm loop + delivery).

### Step 1 — Run the deterministic engine                    [code]
Execute the pipeline. It claims today's slot (idempotency key), loads + validates
config, asks the provider to discover candidates, applies the hard filters, runs
the engine's single AUTO/CONFIRM/BLOCK decision, and on AUTO asks the provider to
place the order (the real provider stops before pay).

**Interpreter:** use the dep-complete Python — the bare `python3` on this machine
(`/usr/bin/python3`) lacks `playwright`, so the DoorDash provider can't import
there. Always invoke `/Users/openclaw/miniconda3/bin/python3`.

**Trigger → command map.** Match the user's phrase to a row and run that command,
prefixing each with the interpreter `/Users/openclaw/miniconda3/bin/python3` (the
bare `python3` lacks `playwright`). Rows marked **🌐 LIVE** open the real DoorDash
browser (real discovery, then the decision). Only the fallback-rescue row uses the
deterministic **mock** provider — a *verified-safe* fallback can't come from a
platform we never trust for safety. Each maps to a `references/failure-modes.md`
(§) row; **Expected** is the engine's verified `(decision, reason, severity)`.
Never use mock to fulfill a real order.

| User says | Command (prefix with the interpreter) | Expected — engine output · `failure-modes.md` § |
|-----------|----------------------------------------|----------|
| `order my daily food` | `run.py --provider doordash --claim-slot --clear-cart --config demo/charlie-unrestricted.yaml` | **LIVE** — carts the chosen dish, **STOPS before pay**, no charge |
| `order my daily food demo fail 1` | `run.py --provider doordash --query thai --config demo/over-budget-live.yaml` | 🌐 **LIVE BLOCK `over_daily_max`** (P1) — browses the real menu; every dish over the $5 cap · **§D** |
| `order my daily food demo fail 2` | `run.py --provider doordash --query thai --clear-cart --config demo/over-auto-live.yaml` | 🌐 **LIVE CONFIRM `above_auto_approve`** (P1) — real ~$18 dish over the $5 auto-approve, asks first · **§D** |
| `order my daily food demo fail 3` | `run.py --provider doordash --query thai --config demo/charlie-no-fallback.yaml` | 🌐 **LIVE BLOCK `unverified_safety`** (P0) — restricted user; DoorDash can't prove a dish is peanut-free · **§C** |
| `order my daily food demo fail 4` | `run.py --provider doordash --query "thai recipe" --dish "pad thai" --config demo/charlie-no-fallback.yaml` | 🌐 **LIVE BLOCK `allergy_violation`** (P0) — orders Pad Thai at Thai Recipe Cuisine; its card declares peanuts → refused. **Needs Thai Recipe OPEN** (closed → `--dish` fails closed → `no_candidate`, still P0-safe) · **§C** |
| `order my daily food demo fail 5` | `run.py --provider mock --scenario allergen --config demo/charlie-trusted.yaml` | **CONFIRM `fallback_in_use`** (P1, mock) — rescued to the safe Chipotle fallback (live fallback is also unverifiable) · **§C** |
| `order my daily food demo fail 6` | `run.py --config demo/invalid.yaml` | **exit 2 `config_invalid`** — fail-loud at config load, no browser by design · **§A** |

(`user_preferences.yaml` holds the user's REAL prefs — peanut allergy, vegetarian.
On live DoorDash those can't be verified safe, so it BLOCKs; use it in place of
charlie-unrestricted only if the user explicitly asks for the "real prefs" run.)

**Live-demo preconditions — verify before demoing:**
- **Run live (doordash) demos ONE AT A TIME.** Each live run holds the single
  warmed Chrome profile; a second concurrent live run fails fast with
  `{"error":"provider_busy"}` (exit 4) instead of a silent timeout. Wait for the
  first to finish before starting the next.
- **fail 4 needs its target restaurant OPEN.** The declared-allergen block is tied
  to one real menu card (Thai Recipe Cuisine's Pad Thai → peanuts). If that store
  is closed, `--dish` fails closed → the engine emits `no_candidate` (P0, still
  safe) instead of `allergy_violation`. Confirm it's open (and still declares
  peanuts), or repoint `--query`/`--dish` to another store whose card declares the
  allergen.

It prints JSON: `{decision:{decision,reason,severity}, placed, order_result, steps}`.
Parse it. Do not recompute the decision.

If it exits non-zero with `{"error":"config_invalid",...}` → the config is unsafe
to use; notify the user with the exact detail, fix nothing silently, place nothing.
If `{"error":"provider_unavailable",...}` (DoorDash bot wall / not logged in) →
notify the user to re-run `--login`; place nothing.
If `{"error":"provider_busy",...}` (exit 4 — another live run already holds the
browser profile) → do NOT retry immediately; wait for the in-flight run to finish,
then re-run. Place nothing. (Live demos run one at a time.)

### Step 2 — Resolve the verdict                              [agent + code]
Read `decision.decision`:

- **AUTO** → already placed (mock) or carted-and-stopped-before-pay (DoorDash).
  Go to Step 3 and send the calm "ordered" notification.
- **CONFIRM** → nothing placed yet. Notify the user (severity-calibrated) with
  what would be ordered and why it needs a yes (cost band / rolling cap /
  fallback-in-use). Wait up to `confirmation_timeout_minutes` (default 20):
  • "yes" → re-run Step 1; on AUTO-equivalent placement, notify.
  • "no"  → acknowledge, place nothing.
  • silence → SKIP. The one exception is `fallback_in_use`, which is
    pre-authorized and may proceed. An over-threshold order never places on
    silence.
- **BLOCK** → not orderable as selected (over daily_max, no compliant/safe
  option, allergen). Do NOT ask the user to approve it. Try the configured
  fallback if present and re-checked safe + ≤ daily_max (treat as a
  fallback-in-use CONFIRM); otherwise send a LOUD notification with the reason
  and place nothing.

### Step 3 — Notify via the `message` tool                    [agent]
Send one severity-calibrated Discord message. Never fully silent; interrupt only
when actionable.

    message → { "action": "send", "channel": "discord",
                "to": "channel:<your-discord-channel-id>",
                "message": "🍱 Ordered Vegetarian Pad Thai from Thai Spice — $14 (auto, within budget)." }

**Self-contained alternative:** `python3 run.py … --notify` posts the same
severity-calibrated summary straight to the Discord webhook in
`$DFO_DISCORD_WEBHOOK` (no agent needed — handy for cron/headless). The webhook
is a secret: keep it in the environment, never in the repo.

Calibrate by severity: P0 (safety) is loud and explicit; P1 (money/wrong order)
is clear; P2 (info) is calm. For DoorDash, say plainly that the order was
**carted and stopped before payment** — no charge was made.

## Payment safety (non-negotiable)

A real account is treated like a live trade: this skill NEVER completes a charge.
The DoorDash adapter stops at the checkout/pay screen and returns
`STOPPED_BEFORE_PAYMENT` with `charged: false`. Completing a real charge would
require ALL of: the `--complete-payment` flag, a typed env confirmation
(`DAILY_FOOD_CONFIRM_CHARGE`), and code wiring this build deliberately omits.
Do not attempt to click "Place Order" through any other tool.

## Triggering a failure path (for testing)

    [code]  python3 run.py --scenario over_budget    # BLOCK over_daily_max
    [code]  python3 run.py --scenario unavailable     # BLOCK (no available option)
    [code]  python3 run.py --scenario empty           # BLOCK no_candidate
    [code]  python3 run.py --scenario allergen        # BLOCK allergy_violation (P0)

## Operating principles

In priority order, every run obeys:
- **Consequence asymmetry** — soft preferences (cuisines, favorites) are
  optimized for and a miss is fine; hard restrictions (dietary, allergies) are
  absolute. A missed preference is an annoyance; a violated restriction is the ER.
- **Standing authority** — act only within pre-authorized limits. Uncertainty,
  silence, and failure are never read as approval; fall back to what is already
  authorized and wait for an explicit yes.
- **Fail loud, never silent** — no failure is dropped; each is surfaced with its
  severity and recovery step, loudness scaled to consequence.
- **Confirm within authority; block at hard lines** — CONFIRM asks for a yes on
  something within limits; BLOCK refuses a hard-line crossing and explains why.
  Never ask the user to approve the unsafe or the over-ceiling.
- **Trust is a dial** — the user raises `auto_approve_under` to grant autonomy
  over time; after a breach the agent lowers its own, scaled to severity.
- **Deterministic safety, optional intelligence** — allergy/dietary/budget logic
  is hard-coded and runs with any LLM or none; the model only ranks and phrases,
  so it can never hallucinate a safety decision.

## Configuration

All behavior is driven by `user_preferences.yaml`; only `daily_max_usd` is
required, everything else is safe-by-default. Six groups: **schedule** (order_time
+ timezone), **budget** (daily_max REQUIRED, auto_approve_under, rolling_cap +
window), **preferences** (SOFT: cuisines, favorite_restaurants — ranking only),
**restrictions** (HARD: dietary, allergies, never_order — filter + safety),
**fallback** (one pre-vetted safe default), **notifications** (channel + confirm
timeout). The default `auto_approve_under: 0` means confirm every order until the
user grants autonomy. No field can disable a safety or budget check. Full field
reference → `references/schema.md`.

## Error handling & fallback

One engine for every failure: **detect → classify severity (P0 safety · P1
money/wrong-order · P2 inconvenience) → resolve (AUTO/CONFIRM/BLOCK) → notify
(scaled) → record.** Headline cases:
- invalid config → fail loud at load, name each fix, place nothing
- no restaurant / no candidate → fallback, else BLOCK + notify
- no compliant/safe option (P0) → safety-checked fallback, else BLOCK + loud notify
- DoorDash bot wall / not logged in → `provider_unavailable`; tell the user to
  re-run `--login`; place nothing
- provider error mid-flight → recorded as `FAILED`, never blind-retried, never
  charged-but-unconfirmed

Full taxonomy → `references/failure-modes.md`. Design rationale →
`references/trust-model.md`.

## Examples

### Example 1 — Happy path (AUTO)
`python3 run.py` → discover (mock) → safe vegetarian $14 ≤ $18 auto → AUTO →
placed. Notify: "🍱 Ordered Vegetarian Pad Thai from Thai Spice, $14."

### Example 2 — Confirm band (CONFIRM)
Best safe option is $21 (between $18 auto and $25 max) → CONFIRM (cost). Notify +
wait 20 min → "yes" → place. On silence it skips.

### Example 3 — Over budget (BLOCK)   [the failure path, live]
`python3 run.py --scenario over_budget` → only option is $99 > $25 max → BLOCK
`over_daily_max`. Nothing placed; notify with the reason.

### Example 4 — Real DoorDash (stops before pay)
`python3 run.py --provider doordash` on a warmed profile → real menu → add item →
checkout → STOP. `order_result.status = STOPPED_BEFORE_PAYMENT`, `charged: false`.
Notify: "🍱 Carted <item> from <restaurant> on DoorDash and stopped before
payment — no charge made."
