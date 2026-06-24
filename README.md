# Daily Food Ordering Agent Skill

A daily meal-ordering agent that aims for safe autonomy through deterministic guardrails: the model may rank and phrase, but safety, budget, and final authority are code.

## Quickstart

```bash
pip install pyyaml pytest        # Windows: also pip install tzdata
python run.py                 # runs the 11-step pipeline on user_preferences.yaml
pytest                        # the engine + config + run tests
```

## Reading `run.py` output

`python run.py` prints the 11-step audit trail for one demo run.

The key fields are:

- `selected_candidate` - the meal the agent chose from discovery.
- `decision` - the deterministic authority result: `AUTO`, `CONFIRM`, or `BLOCK`.
- `reason` - why the engine made that decision, such as `within_auto_approve`, `above_auto_approve`, or `allergy_violation`.
- `severity` - notification/recovery level. `P0` is safety, `P1` is money/order risk, and `P2` is low-stakes info.
- `placed` - in this skeleton, means "would place." No real food order or payment happens.

For the default config, the demo returns `AUTO / within_auto_approve` because the selected meal is verified safe and costs `$14`, which is under `auto_approve_under_usd`.

## What's here

Start with `SKILL.md`; it is the primary deliverable and the source of the product contract.

- Presentation/report: [view rendered HTML](https://raw.githack.com/charlieyang1557/daily-food-ordering/main/presentation.html) ([source](presentation.html))
- `SKILL.md` - the product/spec contract for the daily food-ordering skill.
- `engine/` - the deterministic core: config validation, data models, and AUTO / CONFIRM / BLOCK decision logic.
- Provider adapters - planned boundary for mock and real food-ordering providers. This is not wired yet.
- `run.py` - an 11-step walking skeleton that loads config, discovers a stub candidate, runs the decision engine, and records the step outcomes.
- `references/` - supporting docs: `trust-model.md`, `failure-modes.md`, and `schema.md`.

The split is deliberate: safety, budget, and decision checks are code, so they run with any LLM or with no LLM at all. A model can help with ranking candidates or writing notifications, but it never gets to decide whether an unsafe or over-budget order is allowed.

## Key Design Decisions

The fuller rationale lives in the decision table in `references/trust-model.md`. The headline choices:

- **Tiered decisions:** every run resolves to `AUTO`, `CONFIRM`, or `BLOCK`. Routine orders can proceed; cost-band or rolling-cap cases ask; hard-line crossings refuse.
- **Consequence asymmetry:** preferences are soft, restrictions are hard. Missing a favorite cuisine is fine. Violating an allergy is not.
- **Deterministic safety:** allergy, dietary, budget, and `never_order` checks are hard-coded and re-checked at decision time.
- **Trust is a dial:** the user grants autonomy with `auto_approve_under_usd`; after a serious breach, the agent should self-throttle until the user restores trust.

## Assumptions

- A mock provider is used for development because no open-source food API covers real menus, payment, and order state well enough for this exercise.
- v1 handles one daily meal slot.
- Notifications are runtime-bound. The config names a channel, but the runtime decides how to send messages.
- Provider candidates should use standardized allergen and dietary tags.
- `verified_safe` is set by the provider. Candidates default to unverified until the provider confirms safety metadata is complete.

## What I'd Build Next

- Multi-meal slots, using the same claim/idempotency model.
- A real provider adapter, such as Yelp or DoorDash behind the same interface.
- Persisted graduated-autonomy state for rolling caps, incidents, and self-throttling.
- A config wizard that suggests local restaurants and safe fallback choices.
- Dietary-uncertainty opt-in confirmation. v1 drops uncertain candidates instead.
- Cost-band downgrade behavior. v1 keeps the timeout rule simpler: skip on silence unless the fallback is already authorized.

## Scope - Deliberately Not Included

This version favors depth over breadth. It proves the trust boundary first: config validation, hard restrictions, budget authority, and the final decision engine.

The following are intentionally out of scope for v1:

- Real payments or real order placement. Those need a provider integration and idempotent reconciliation.
- A dedicated `filter_safe` pre-pass. Safety is already enforced deterministically inside `decide()` (allergy, dietary, `never_order`, fully tested). Pulling it into a separate pre-filter that drops unsafe candidates before ranking is a clean architecture refinement, not a safety gap.
- Persistent ledger storage. The skeleton returns step records in memory.
- A production scheduler. The skill can be triggered externally; `run.py` is the demo entry point.
- Rich notification flows. The decision output contains the reason and severity, but channel delivery is left to runtime wiring.

Those cuts are the point: the dangerous parts are deterministic and tested before the plumbing gets fancy.
