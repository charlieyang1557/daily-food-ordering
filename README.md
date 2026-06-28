# Daily Food Ordering Agent Skill (v2)

An OpenClaw skill that orders the user's single daily meal with **safe autonomy**:
the model may rank and phrase, but safety, budget, and the final order decision
are deterministic code. v2 adds a **real DoorDash provider** (Playwright) behind a
formal provider interface — real menu retrieval and a real cart driven to
checkout, **hard-stopping before payment**.

## Quickstart

```bash
pip install -r requirements.txt
python -m playwright install chromium     # for the real DoorDash provider

python run.py                 # mock provider, happy path (dry & safe)
python run.py --scenario over_budget       # trigger the budget-exceeded failure path
pytest                        # 92 tests: engine + config + run + providers + adapter
```

Install as an OpenClaw skill on this machine (copies the skill + registers the
disabled daily cron trigger):

```bash
bash install.sh
```

## Architecture (what runs)

```
config (user_preferences.yaml)
        |
        v
   run.py  -- the 11-step orchestrator; the skill execs this, the LLM does not re-decide
        |
        |-- Provider.discover(config) -------------+
        |      MockProvider  (deterministic, tests) |   provider boundary
        |      DoorDashProvider (Playwright, real)  -+   (providers/)
        |
        |-- engine/  -- load_config + decide()      <- deterministic safety/budget core (unchanged from v1)
        |      AUTO / CONFIRM / BLOCK
        |
        +-- Provider.place_order(...) on AUTO  -> DoorDash stops before pay (charged: false)
```

- **`engine/`** — `config.py` (validation), `models.py` (types), `decision.py`
  (the AUTO/CONFIRM/BLOCK call). This is the safety core; it is **byte-unchanged
  from v1** and runs with any LLM backend or none.
- **`providers/`** — `base.py` (the `Provider` protocol + `OrderResult`),
  `mock.py` (deterministic stand-in + failure scenarios), `doordash.py` (the real
  Playwright adapter).
- **`run.py`** — loads config, asks the provider for candidates, runs the hard
  filters, calls `decide()`, and on AUTO places the order. Prints a JSON audit
  trail.
- **`SKILL.md`** — the OpenClaw skill spec (frontmatter + ordered `[code]`/`[agent]`
  steps). **`references/cron-job.json`** — the daily trigger for OpenClaw's cron.

## What changed from v1

| v1 | v2 |
|---|---|
| Provider was a *planned* seam; discovery was an inline stub in `run.py` | Formal `Provider` interface; `run.py` injects the provider |
| Mock only, conceptual | `MockProvider` (kept, for tests) **+ real `DoorDashProvider`** (Playwright, persistent profile) |
| No real platform | Real DoorDash menu retrieval + cart → checkout, **hard stop before pay** |
| Spec referenced `scripts/*.py` that didn't exist | `[code]` steps exec the real `run.py` |
| Generic notifications | OpenClaw `message` tool → Discord; daily cron in `jobs.json` |
| Frontmatter had `compatibility`, `metadata.author/version` | Trimmed to OpenClaw-recognized keys (`name`, `description`, `license`, `allowed-tools`, `metadata.openclaw`) |

## Reading `run.py` output

`python run.py` prints the 11-step audit trail. Key fields: `decision`
(`AUTO`/`CONFIRM`/`BLOCK`), `reason` (e.g. `within_auto_approve`,
`over_daily_max`, `allergy_violation`), `severity` (P0 safety · P1 money · P2
info), `placed`, and `order_result` (the structured receipt — for DoorDash,
`status: STOPPED_BEFORE_PAYMENT`, `charged: false`).

## Triggerable failure paths

```bash
# charlie-no-fallback.yaml = restricted (vegetarian + peanut, $25) with NO fallback,
# so each scenario shows its clean BLOCK. (The DEFAULT config is the unrestricted
# happy-path demo, under which --scenario allergen has no allergy to violate.)
python run.py --scenario over_budget --config demo/charlie-no-fallback.yaml  # BLOCK over_daily_max  (budget exceeded)
python run.py --scenario unavailable --config demo/charlie-no-fallback.yaml  # BLOCK no_candidate     (item unavailable)
python run.py --scenario allergen    --config demo/charlie-no-fallback.yaml  # BLOCK allergy_violation (P0 safety)
python run.py --provider doordash --headless   # provider_unavailable (bot wall) — graceful
```

## Payment safety (a real account = a live trade)

The skill **never completes a charge.** The DoorDash adapter drives the cart to
the checkout/pay screen, captures the order summary + a screenshot, and returns
`STOPPED_BEFORE_PAYMENT`. There is **no code path that clicks "Place Order"** —
`_complete_payment` is deliberately unwired and raises. Even the `--complete-payment`
flag plus the `DAILY_FOOD_CONFIRM_CHARGE` env confirmation only passes the gates;
the actual charge is not implemented in this build.

## Assumptions & scope decisions

- **DoorDash needs a human-warmed profile.** doordash.com is behind a Cloudflare
  human-check and shows menus only to a logged-in session with a delivery
  address. Headless/fresh browsers are hard-walled (verified during scoping). So
  the adapter uses a **persistent Chrome profile**: a human runs
  `python run.py --provider doordash --login` once. Cold runs detect the wall and
  raise `ProviderUnavailable` — an expected, recorded failure, never a silent
  proceed.
- **Real DoorDash candidates are `verified_safe=False`.** DoorDash can't reliably
  confirm allergens/dietary tags, so for a user *with* restrictions the engine
  correctly **BLOCKs** real candidates as `unverified_safety`. This is
  safety-first behavior, not a bug. Unrestricted users get AUTO/CONFIRM by price.
- **Selectors are DOM-dependent.** They are centralized in `providers/doordash.py`
  with layered fallbacks (`data-anchor-id` / `data-testid` / role+text);
  `DoorDashProvider.diagnose()` dumps live markers to re-tune them against a real
  logged-in session.
- **The cron trigger ships disabled.** `references/cron-job.json` (and `install.sh`)
  register the job with `enabled:false`; it never fires until a human flips it on
  after logging in.
- `engine/` is the trust boundary and is unchanged; `verified_safe` is set by the
  provider; v1 handles one daily meal slot.

## What I'd build next

- **Broaden add-to-cart across every store layout** — `place_order` already
  carries the store URL through discovery, navigates into the dish's store,
  completes the customization modal (radio / checkbox / select required groups),
  and carts before hard-stopping at the pay screen. It's reliable on verified
  stores; some stores' modal layouts still need coverage — tune against more live
  carts and add a per-layout fallback.
- Parse the full checkout breakdown (line items, fees, tax, tip) into
  `OrderResult.summary`. *(The total is already reconciled against `daily_max`,
  failing closed if missing or over — see `_reconcile_budget`.)*
- A **persisted spend ledger** so `rolling_cap_usd` is actually enforced. Today
  it is **not**: `decide()` accepts a rolling total, but `run()` has no spend
  history to pass, so it sends `0` and the cap never fires. Enforcing it needs a
  durable ledger of placed-order spend summed over the rolling window. *(Per-day
  idempotency, by contrast, **is** durable — `--claim-slot` writes a state-aware
  atomic marker under `~/.daily-food-ordering/slots/` and only a placed order
  consumes the day.)*
- Distinguish "sold out → P2" from "no safe option → P0" at the run level (today
  an all-unavailable set collapses to `no_candidate` after filtering).
- A second provider (UberEats/Yelp) behind the same interface to prove
  swappability, and a config wizard that suggests local safe fallbacks.

## Scope — deliberately not included

- Completing a real payment (intentionally unwired — see Payment safety).
- Defeating Cloudflare via fingerprint spoofing (fragile and bad-faith; the
  warmed-profile path is the honest answer).
- A production scheduler beyond OpenClaw's cron; multi-meal slots.

The cuts are the point: the dangerous parts — safety, budget, the order decision,
and the payment stop — are deterministic, tested, and proven against the real
platform before the plumbing gets fancy.

The fuller design rationale lives in `references/trust-model.md`; the full config
schema in `references/schema.md`; the failure taxonomy in
`references/failure-modes.md`.
