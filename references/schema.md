# Trust model — how the agent decides
this is the reasoning behind SKILL.md. The instructions are the *what*; this is the *why*.

## The decision engine
Every run resolves to exactly one verdict — AUTO / CONFIRM / BLOCK. One path, not ad-hoc rules.
AUTO - order cost ≤ auto_approve and within rolling-cap and verified-safe
CONFIRM - when encounter uncertainty (like over auto-approved budget but under daily budget) and need escalate to user interface for extra layer of security, fires on cost-band or rolling-cap or fallback-in-use
BLOCK - ordering would cross a hard line (over daily_max, or no compliant option). Refuse + notify why; place nothing.

## Two governing principles
- **Consequence asymmetry** — a missed preference is an annoyance; a violated restriction is the ER. Different cost → different machinery.
- **Standing authority** — acts only within pre-authorized limits; on uncertainty / silence / failure, falls back to what's authorized, never escalates spend or risk.

## Budget model
auto_approve_under (per-order authority)· daily_max (hard ceiling, never crossed) · rolling_cap+window (cumulative authority).
Then the bands: ≤auto → AUTO · auto–max → CONFIRM · >max → BLOCK.

## Confirmation model
Triggers: cost band · rolling-cap · fallback-in-use. (Note: dietary-uncertainty is
dropped in v1 — uncertain candidates are filtered out, not confirmed.) Timeout rule:
on silence → skip; the one exception is fallback-in-use (pre-blessed → proceed).

## Restrictions vs. preferences
Hard = filter + gate (never relaxed; uncertain → dropped). Soft = rank only (a miss
is fine).

## Trust as a dial (graduated autonomy)
Earned up: the user raises auto_approve over time. Spent down: after a breach the
agent self-throttles, severity-scaled, until the user restores it.

## Post-order recovery (when prevention fails)
The only post-order path: detect (self-audit + user report) → contain harm (P0
"don't eat" > refund) → undo if possible → own it (admit before refund) →
self-throttle → learn. Severity inherited from what was wrong.

## Deterministic safety, optional intelligence
Safety logic (allergy/dietary/budget) is code → runs with any LLM or none; the model
only ranks + phrases → can never hallucinate a safety decision. This is how the skill
is backend-agnostic.

## Key decisions & rationale

| Decision | Why |
|---|---|
| Over-`daily_max` / no-safe-option → BLOCK, not CONFIRM | You never ask a human to approve crossing a hard line — there's nothing to confirm. CONFIRM is within-authority; BLOCK is the hard line. |
| Safety checks hard-coded in scripts, not the LLM | The model can't hallucinate a safety decision if safety isn't its job — and it's what lets the skill run with any backend, or none. |
| Replaced the starter's `confirmation_required` bool with `auto_approve_under` (default 0) | A blunt on/off can't be "autonomous but safe"; a threshold lets confirmation scale with trust instead of nagging every order. |
| Mock the provider behind a clean adapter interface | No open-source food API does real payments; mocking lets you trigger every failure on demand, keeps zero proprietary deps, and proves the design is backend-agnostic/swappable. |
| Tiered AUTO / CONFIRM / BLOCK | Resolves the `confirmation_required` vs. "no per-run input" tension — safe by default without nagging. |
| `auto_approve_under` separate from `daily_max` | Splits per-order authority from the hard ceiling — routine orders stay silent; only the band confirms. |
| `rolling_cap` + window | Bounds cumulative blast radius over time, which a per-order cap can't. |
| Soft preference vs. hard restriction (structural split) | Consequence asymmetry — a missed preference is annoyance, a violated restriction is the ER → different machinery. |
| Stateful ledger = memory primitive | Rolling-cap, idempotency, learning, and graduated autonomy all need persisted state. |
| Pluggable trigger + reference scheduler | A skill is invoked, not a daemon; an external scheduler fires it, and a bundled cron makes it demoable solo (fits Ghost). |
| Idempotency: one order per day-slot (skill claim + provider key) | Kills double-orders from manual+scheduled, retries, and crashes; the key also enables partial-failure reconciliation. |
| Second-order routes on trigger source; never autonomously place #2 (v1: report + stop) | Distinguishes a duplicate from an intended second order without mind-reading; eliminates "confirm twice" confusion. |
| Standardized vocab (FDA Big-9, etc.) | Free text degrades the safety filter into guesswork; a controlled vocab is what makes it trustworthy. |
| Negatives split by enforceability (`never_order` = restaurant/cuisine; dislikes = soft) | Never promise to enforce what you can't verify — ingredient data is unreliable. |
| Single daily slot in v1; multi-meal in v2 | Scoped to the spec ("once per day"); the slot model already generalizes. |
| One failure engine (detect → severity → resolve → notify → record) | One decision path, not 20 special cases; the matrix reuses the trust engine. |
| Timeout = standing authority: skip on silence, except fallback proceeds | "Silence is never a yes" — one memorable rule, no surprise substitution (dropped the cost-band downgrade). |
| Post-order recovery: contain-harm → undo → own-it → self-throttle → learn | The only post-order failure is recovery, not prevention; self-throttling (asking for less power after a breach) is "earning trust" made literal. |
| Schema: only `daily_max` required, safe-by-default, no safety-disable knob | Rubric-#3 (minimal setup, no per-run input) + deliberate omission of dangerous knobs. |