# Failure modes — full taxonomy

The complete reference. SKILL.md carries only the headline cases; this is the exhaustive list.

## The engine
Every failure runs one path: **detect → classify severity → resolve → notify → record.**
- **Severity:** P0 safety (allergy/dietary) · P1 money / wrong-order · P2 inconvenience/quality
- **Resolve:** AUTO-handle · CONFIRM · BLOCK / skip
- **Notify:** loudness scaled to severity — P0 interrupts, P2 is a quiet log; never fully silent
- **Record:** every outcome to the ledger (idempotency, audit, rolling-cap, learning)

## Governing rules
- On uncertainty or silence → fall back to standing authority; never escalate spend or safety risk.
- Never relax a hard restriction to fill the set.
- Never place a charged-but-unconfirmed or duplicate order (the idempotency key guards both).
- Never ask the user to approve crossing a hard line — that's a BLOCK + notify, not a CONFIRM.

## A · Config load  (validated at load; fail loud, place nothing)
| mode | detect | resolve |
|---|---|---|
| Invalid timezone (not IANA) | load | reject + name the fix ("America/EST → America/New_York"); halt |
| Non-standard allergen token | load (vs FDA Big-9) | reject, or normalize to closest + confirm; halt until fixed |
| Ingredient in `never_order` | load (granularity) | reject, or reclassify as a soft dislike |
| Missing `daily_max` | load | reject + halt — can't run without a spending limit |
| `auto_approve_under` > `daily_max` | load | reject + halt — contradictory |

## B · Trigger / schedule
| mode | severity | resolve |
|---|---|---|
| Missed run (host down at order_time) | P2 | on late wake: order if still timely, else skip + notify |
| Duplicate / concurrent trigger | — | idempotency: slot already claimed → scheduled no-ops, manual reports |
| DST / clock edge | — | handled by IANA tz (never abbreviations) |

## C · Discovery / selection
| mode | severity | resolve |
|---|---|---|
| No restaurant open / nothing orderable | P0* | try fallback (→ CONFIRM `fallback_in_use`) → else **BLOCK `no_candidate`** + notify |
| Preferred cuisine unavailable | P2 | degrade through ranked cuisines → fallback → skip |
| **No compliant / safe option** | **P0** | safety-checked fallback → **BLOCK + loud notify** if none. **NEVER relax** |
| Favorite / pinned restaurant unavailable (closed, out of range, no match) | P2 | discovery degrades to the next available store and orders there; surfaced **honestly** in `summary.degradation_reason` ("preferred restaurant(s) not available for this order — ordered from the next available store") — reported only when the carted store matches **no** favorite and **not** the pre-vetted fallback (it states the fact, not an unverified "closed" cause) |
| Can't verify dietary compliance | P0 | mark uncertain → dropped in v1 (`unverified_safety`, filtered, never silently kept) |
| Item flagged unavailable (reaches decide) | P2 | `unavailable` — but run.py filters these pre-decision, so the reachable empty-set outcome is `no_candidate` (P0) |

\* The engine emits an empty selection as **`no_candidate` (P0)** — it can't
distinguish "nothing open" (inconvenience) from "nothing *safe*" (P0), so it
**fails safe** at P0 rather than under-warning. With a fallback it's rescued to
CONFIRM `fallback_in_use` (P1) instead.

## D · Budget / decision
| mode | severity | resolve |
|---|---|---|
| Cheapest compliant > `daily_max` | P1 | BLOCK + notify ("all over budget — raise the limit or skip") |
| Cost in confirm band (auto < price ≤ max) | P1 | CONFIRM |
| Rolling cap would be exceeded | P1 | CONFIRM (autonomous authority exhausted) |

## E · Execution / ordering
| mode | severity | resolve |
|---|---|---|
| API down / timeout | P1 | retry N× with idempotency key → skip if still failing (never charged-but-unconfirmed) |
| Order rejected (item OOS) | P2 | re-pick → fallback → skip |
| Payment declined | P1 | BLOCK + notify ("update payment"); no retry, no card-swap |
| **Partial failure (charged, state unknown)** | **P1** | reconcile via the key; never blind-retry; unresolved → loud, honest escalation |
| Concurrent live run (shared browser profile) | — | advisory flock on the profile dir: the 2nd run (incl. `--login`) fails fast → `provider_busy` (exit 4), never a silent timeout — run live demos sequentially |

## F · Confirm / notify
| mode | severity | resolve |
|---|---|---|
| Can't reach user | — | if a confirm is needed and the user is unreachable → fail safe (skip) |
| Confirmation timeout | — | standing authority: skip — *except* fallback-in-use, which proceeds |
| User declines | — | skip + acknowledge |

## G · Post-order
| mode | severity | resolve |
|---|---|---|
| **Wrong order** (self-audit or user report) | inherited | contain harm (P0 "DO NOT EAT") → undo → own it → self-throttle → learn |
| Restaurant substitution (kitchen swap) | inherited | same path — this is *why* the post-order self-audit exists |
| Delivery late / missing | P2 | notify / hand off to support (out of core scope) |

## Worked example — flawed config caught (Emily)
Emily's config loads with three flaws; all caught at **Stage A**, run halts, nothing ordered:
- `timezone: America/EST` → invalid IANA → *"did you mean America/New_York?"*
- `allergies: [tuna]` → not in FDA Big-9 → reject / normalize to `fish`
- `never_order: [cilantro]` → ingredient, not restaurant/cuisine → reclassify as a soft dislike

Result: one message naming all three fixes; places nothing.

## Demo cases (verified against the engine)

The `order my daily food demo fail N` triggers (SKILL.md) each exercise one row
above. **Expected** is the engine's verified emitted `(decision, reason, severity)` —
the demo, the engine, and this taxonomy all agree.

| Trigger | mode · config | § | Engine emits |
|---|---|---|---|
| fail 1 | 🌐 LIVE · doordash + `over-budget-live.yaml` (daily_max $5) | D | **BLOCK** `over_daily_max` (P1) |
| fail 2 | 🌐 LIVE · doordash + `over-auto-live.yaml` (auto $5 / max $50) | D | **CONFIRM** `above_auto_approve` (P1) |
| fail 3 | 🌐 LIVE · doordash + `charlie-no-fallback.yaml` (restricted) | C | **BLOCK** `unverified_safety` (P0) |
| fail 4 | 🌐 LIVE · doordash `--dish "pad thai"` at Thai Recipe (card declares peanuts) | C | **BLOCK** `allergy_violation` (P0) |
| fail 5 | mock · `allergen` + trusted config (Chipotle fallback) | C | **CONFIRM** `fallback_in_use` (P1) |
| fail 6 | config load · `demo/invalid.yaml` | A | exit 2 `config_invalid` |

fail 1–4 run **live** on DoorDash (the browser opens, real discovery → decision).
fail 4 targets a dish whose card *declares* an allergen (Pad Thai → peanuts) with
`--dish`, so the engine emits a precise `allergy_violation` rather than the generic
`unverified_safety` — but **only while Thai Recipe Cuisine is open** (closed →
`--dish` fails closed → `no_candidate`, P0-safe, not `allergy_violation`). Live
(doordash) demos run **one at a time**: the shared warmed Chrome profile is locked,
so a concurrent live run returns `provider_busy` (exit 4), never a silent timeout.
Only fail 5 (fallback rescue) uses the **mock** provider —
a *verified-safe* fallback can't come from a platform we never trust for safety.
fail 6 fails at config load (no browser). `order my daily food` (no `fail N`) is the
live run that carts a dish and stops before pay.