# Configuration schema — full reference

the complete field-by-field spec; SKILL.md's Configuration is the summary.

## Fields

### schedule
| field | type | required / default | meaning |
|---|---|---|---|
| order_time | "HH:MM" | default "12:00" | when to run and place the order |
| timezone | IANA string | default: system | user's local zone, IANA name (e.g. America/Chicago) |

### budget
| field | type | required / default | meaning |
|---|---|---|---|
| daily_max_usd | number | REQUIRED | hard ceiling, never exceeded |
| auto_approve_under_usd | number | default 0 | ≤ → AUTO; 0 = confirm all |
| rolling_cap_usd | number | default: disabled | cumulative autonomous cap |
| rolling_window_days | int | default 7 | window for the cap |

### preferences  (soft — ranking only; a miss is fine)
| field | type | required / default | meaning |
|---|---|---|---|
| cuisines | list of strings | [] | ranked cuisine preferences, best first; free-form |
| favorite_restaurants | list of strings | [] | preferred spots; boosts rank |
| dislikes | list of strings | [] | ingredients/dishes to down-rank; penalty only — never a block or confirm |

### restrictions  (hard — filter + safety; never relaxed)
| field | type | required / default | meaning |
|---|---|---|---|
| dietary | list (controlled vocab) | [] | filters out non-compliant options |
| allergies | list (FDA Big-9) | [] | hard safety filter; unverifiable options dropped, never kept |
| never_order | list (restaurant/cuisine) | [] | always-exclude; enforceable granularity only (not ingredients) |

### fallback
| field | type | required / default | meaning |
|---|---|---|---|
| restaurant | string | none → skip | pre-vetted safe default; must be restriction-safe and ≤ auto_approve_under; re-checked each use, never blind-trusted |

### notifications
| field | type | required / default | meaning |
|---|---|---|---|
| channel | string | runtime-bound (e.g. "discord") | how the agent reaches the user |
| confirmation_timeout_minutes | int | 20 | how long CONFIRM waits; on timeout → skip (fallback-in-use is the exception → proceed) |

## Allowed vocabularies
Controlled — safety-critical, must match exactly; free text is rejected:
- dietary: vegetarian, vegan, halal, kosher, pescatarian, gluten-free, dairy-free
- allergies (FDA Big-9): milk, eggs, fish, shellfish, tree nuts, peanuts, wheat, soy, sesame

Suggested — free-form, ranking only, NOT enforced:
- cuisines (examples): Italian, French, Mexican, Chinese, Indian, Japanese, Thai,
  Spanish, Mediterranean, Vietnamese, Korean, Southeast Asian, West African, American

## Validation (fails loud at load)
- missing `daily_max` · `auto_approve_under > daily_max` · bad IANA tz ·
  non-standard allergen · ingredient in `never_order`

## Not configurable
- No field disables a safety or budget check.