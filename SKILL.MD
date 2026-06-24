---
name: daily-food-ordering
description: >
  Use this skill to order a daily meal for the user. It runs automatically
  when the user's configured order_time arrives. Also use it when the user
  says "order food" or "place an order," or "set up daily food ordering," or
  asks to change or update the current config.

  It works within a per-meal budget set by the user. To avoid asking every
  time, the user can set an auto-approve threshold — orders under it go
  through automatically. When the agent is uncertain or can't decide, it
  either asks the user to confirm or falls back to their pre-set backup,
  scaled to severity. It never spends beyond the limits the user set, never
  acts outside the authority the user granted, and never orders food that
  violates the user's dietary or allergy restrictions.

  It reads config from user_preferences.yaml. Do NOT use it for ordering
  anything other than the user's own single daily meal (e.g. team lunches
  or group orders).
license: MIT
compatibility: >
  Backend-agnostic — works with any LLM backend, or none (the deterministic
  safety logic runs without a model). Requires Python 3.11+ and network
  access to a food-ordering provider.
allowed-tools: Bash(python:*)
metadata:
  author: Charlie Yang
  version: 0.1.0
---

## Instructions

When invoked, follow these steps in order. Steps marked [code] MUST call the
deterministic scripts — do NOT reason about budgets, restrictions, or the order
decision yourself. Steps marked [llm] may use model judgment.

### Step 1 — Claim today's slot                              [code]
Compute "today" in the user's tz; check the ledger:
- Slot already claimed / order exists today → no-op, log "already ran," EXIT.
- Else claim it: write a pending marker + a fresh idempotency key → continue.
Run: python scripts/claim_slot.py --date {today} --tz {user_tz}
Ledger unreachable → do NOT proceed (blind run risks a double order); notify + stop.
→ Produces the idempotency key reused by Steps 9 and 10.

### Step 2 — Load & validate config                          [code]
Read user_preferences.yaml: meal time + timezone, budget + auto-approve budget,
preferred cuisines + restaurants, and the dietary restrictions + food allergies
(so we are not sending the user to the hospital). Store parsed values to JSON for
tracking. Then validate before continuing:
- daily_max missing → fail loud + stop (the one REQUIRED field).
- auto_approve_under > daily_max (contradiction) → fail loud + stop.
- unknown timezone / non-standard allergen → fail loud.
Run: python scripts/load_config.py --path user_preferences.yaml

### Step 3 — Discover candidates                             [code+llm]
Search the provider for the user's preferred cuisines + restaurants within
delivery range, filtered to open-now (open at order_time in the user's tz).
Run: python scripts/discover.py --tz {user_tz} --at {order_time}
If nothing comes back → this is the ⭐"no restaurant open" failure → route to
Step 4's fallback/empty-set handling.

### Step 4 — Filter by hard restrictions                     [code]
Drop anything that violates a hard line — allergies + dietary first, then
never_order. Mark any candidate whose compliance can't be verified as
"uncertain" (do not silently keep it). Safety only — budget is Step 6.
- Dietary-uncertainty > drop them
Empty-set branch (the headline job of this step):
- If the safe set is EMPTY → this is the P0 "no compliant option."
  → restriction-safe fallback (re-checked), else BLOCK + loud notify.
- NEVER relax a hard restriction to fill the set.
Run: python scripts/filter_safe.py

### Step 5 — Rank by soft preferences                        [llm]
Use the model to rank the safe candidates by user preference (cuisine order,
favorite restaurants, dishes, flavors). Apply a ranking penalty to dislikes —
penalty only, never a block or confirm. A miss here is OK.

### Step 6 — Select & price                                  [code]
Price the ranked candidates; keep the in-budget set (≤ daily_max). Select the
best-ranked option within budget; among ties, prefer the cheaper one.
If NO candidate is within budget (cheapest compliant > daily_max) → flag for
BLOCK at Step 7. Do not pick an over-ceiling option.
Run: python scripts/select_price.py

### Step 7 — Decide (AUTO / CONFIRM / BLOCK)                 [code]
Re-run all hard checks on the selected candidate (catch anything missed), then:
- AUTO iff (≤ auto_approve and <= rolling-cap and verified-safe)
- auto_approve_under < price ≤ daily_max      → CONFIRM
- cumulative + this order > rolling_cap → CONFIRM
- violates a hard line (over daily_max, dietary, allergies) → BLOCK
Run: python scripts/decide.py
Returns: {decision, reason, severity}.

### Step 8 — Resolve the decision        [code decides · llm composes]
AUTO → place (Step 9).

CONFIRM → notify (severity-calibrated) + wait (confirmation_timeout = 20 min):
  • "yes"    → place (Step 9)
  • "no"     → skip + acknowledge
  • timeout  → on silence, do ONLY what's already authorized:
       fallback-in-use → place the fallback (pre-authorized: safe + ≤ auto_approve)
       everything else (cost band · rolling-cap · dietary-uncertain) → skip
       — an over-threshold or unverified order never places without an explicit "yes"

BLOCK → not orderable as selected. Try the configured fallback:
  • fallback present & re-checked safe + ≤ daily_max → treat as a fallback-in-use CONFIRM (above)
  • no safe fallback → loud notify + skip   ← NOT "confirmation": there's no safe order to approve

### Step 9 — Place the order                                 [code]
Using the idempotency key from Step 1:
- API down / timeout → retry N× WITH the key → still failing → skip
  (never charged-but-unconfirmed).
- payment declined → BLOCK + notify ("update your payment"); do NOT retry the
  dead card, do NOT swap to another card.
- partial failure (charged, state unknown) → do NOT blind-retry; reconcile via
  the key ("did order <key> go through?"); unresolved → loud, honest P1
  escalation ("I may have been charged ~$X but can't confirm — please check;
  I'm not retrying, to avoid a double charge").
Run: python scripts/place_order.py --key {idempotency_key}

### Step 10 — Post-order self-audit                          [code]
Detect: re-run the hard checks against the FINAL placed order (+ accept a
user-reported "this is wrong"). If a violation surfaces, recover by INHERITED
severity (allergy = P0, over-budget = P1, taste/dislike = P2):
1. Contain harm first — for P0, notify loudly and clearly: "DO NOT EAT"
   (this outranks the refund).
2. Undo if possible — attempt to cancel within the provider's window.
3. Own it — plain admission FIRST ("I ordered X, which violates your shellfish
   restriction. My mistake."), THEN issue the refund/credit. A silent refund
   is not owning it.
4. Self-throttle — after a P0 breach, the agent drops its OWN auto_approve
   toward 0 (confirm-everything) and tells the user: "I've paused autonomous
   ordering until you re-enable it." Severity-scaled: a P2 taste-miss just
   learns; a P0 breach pauses autonomy. Asking for less power IS the apology.
5. Learn — log the episode to the ledger; blocklist the item/restaurant and
   distrust the menu source that gave bad data, so it can't recur.

### Step 11 — Record & notify                               [code]
Record every step + result to the ledger (idempotency, audit, rolling-cap,
learning). Push one severity-calibrated notification: what was ordered and why
(AUTO / CONFIRMED / BLOCKED, preferred pick or fallback). Never fully silent;
interrupt only when actionable.
Run: python scripts/record_notify.py
---

## Operating principles
Every run obeys these, in priority order:
- **Consequence asymmetry** — Soft preferences (cuisines, favorite/disliked spots) are optimized for; a miss is fine. Hard restrictions (dietary, allergies) are absolute: a missed preference is a mild annoyance, but a violated restriction breaks the user's ethics or sends them to the ER.
- **Standing authority** — The agent acts only within the limits the user pre-authorized. It never reads uncertainty, silence, or failure as approval — it falls back to what's already authorized and waits for an explicit yes. Uncertainty is never a yes.
- **Fail loud, never silent** — No failure is ever dropped silently; every one is recorded. The agent surfaces failures with their severity and recovery step, loudness scaled to consequence — a P0 interrupts, a P2 is a quiet log.
- **Confirm within authority; block at hard lines** — CONFIRM asks for a yes on something within limits; BLOCK refuses a hard-line crossing (over-budget, unsafe) and notifies with the reason. The agent never asks the user to approve the unsafe or the over-ceiling.
- **Trust is a dial** — Autonomy moves both ways: the user raises auto_approve to grant trust over time; after a breach the agent lowers its own, scaled to severity, until the user restores it. Earned up, spent down.
- **Deterministic safety, optional intelligence** — Safety logic (allergies, dietary, budget) is hard-coded and runs with any LLM or none; the model only ranks and phrases, so it can never hallucinate a safety decision.
---

## Configuration

all behavior is driven by user_preferences.yaml; only daily_max is required, everything else is safe-by-default.

Six groups:
- **schedule** — when it runs: order_time + timezone, user-local; defaults to system tz
- **budget** — daily_max (REQUIRED), auto_approve_under, rolling_cap + window
- **preferences** (SOFT) — cuisines, favorite restaurants → used for ranking
- **restrictions** (HARD) — dietary, allergies, never_order → used for filtering + safety
- **fallback** — one pre-vetted safe default
- **notifications** — channel (pluggable)+ confirmation timeout

**Required:** Only daily_max is required. Everything else is safe-by-default, so the agent works from minimal setup and never interrogates the user field-by-field on each run. Its default — auto_approve_under: 0 — means confirm every order until the user grants autonomy. This is the deliberate resolution of the starter schema's confirmation_required flag: instead of a blunt on/off, confirmation becomes a spending threshold the user dials up as trust grows.

Soft vs. hard — Preferences rank; restrictions gate. A soft miss is fine; a hard line is never crossed.

No field can disable a safety or budget check — those aren't configurable.

Full field reference, types, defaults, and allowed values (cuisines, dietary terms,
FDA Big-9 allergens) → references/schema.md.
---

## Error handling & fallback

Every failure runs one engine, not ad-hoc handling: **detect → classify severity (P0 safety · P1 money/wrong-order · P2 inconvenience) → resolve (AUTO / CONFIRM / BLOCK) → notify (scaled to severity) → record.**

Per-failure handling is inline in the Instructions. Headline cases:
- no restaurant open (P2) → fallback → skip if none → BLOCK + notify
- no compliant/safe option (P0) → safety-checked fallback → BLOCK + loud notify if none,
- API down → retry + idempotency key → skip · payment declined → BLOCK ·
  partial failure → reconcile, never double-charge
- wrong order, caught post-order → contain harm → undo → own it → self-throttle → learn

Governing rule: on uncertainty or silence, fall back to standing authority; never escalate spend or safety risk; never fully silent.

invalid config (bad timezone / non-standard allergen / ingredient in never_order) → fail loud at load, name each fix, place nothing

Full failure taxonomy (~20 modes across config / discovery / budget / execution / post-order) → references/failure-modes.md.
---

## Examples

### Example 1 — Happy path (AUTO)
Trigger: scheduled 12:30, Charlie's config
Actions: claim slot → discover → filter (vegetarian, no peanuts) → rank →
          select $14 (≤ $18 auto) → AUTO → place → record
Result:  ordered silently; one calm notification: "Ordered Pad Thai from Thai Spice, $14"

### Example 2 — In the confirm band (CONFIRM)
Trigger: best safe option is $21 — between auto_approve $18 and daily_max $25
Actions: claim slot → discover → filter (vegetarian, no peanuts) → rank →
          select $21 (between $18 and $25) → decide = CONFIRM (cost) → notify + wait 20 min → user "yes" → place
Result:  ordered after one confirmation; on silence it skips — an over-auto-approve order never places without an explicit "yes."

### Example 3 — No safe option (BLOCK)              [the safety engine, live]
Trigger: scheduled lunch run for Charlie. Restaurants ARE open — but every
         open option is peanut-risk or can't verify peanut-free, AND Chipotle
         (his fallback) is closed today.
Actions: claim → discover (open-now: results EXIST) → filter: safe set EMPTY
         → re-check fallback → fallback unavailable too → no safe option anywhere → BLOCK
Result:  nothing ordered; LOUD notify — "Skipped today: nothing safe for your
         peanut allergy was available and your backup was closed. I won't relax
         that. Widen range or pick manually?"