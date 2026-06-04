# RLEF — Reinforcement Learning from Execution Feedback

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SQLRefactorEnv                               │
│                                                                     │
│  reset(instance)                                                    │
│    ├── Database.describe_schema()  → full normalized schema         │
│    ├── pg_stat_user_tables         → row counts, scan stats         │
│    └── EXPLAIN (FORMAT JSON)       → baseline plan cost             │
│         ↓                                                           │
│  Observation: { schema, db_stats, baseline_sql, baseline_cost }     │
│                                                                     │
│  step(action)                                                       │
│    ├── Parse <reasoning>...</reasoning> + SQL                       │
│    └── compute_reward(optimized, baseline, gold, engine, shadow)    │
│         ├── EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)                 │
│         │    ├── total_cost → cost ratio reward                     │
│         │    ├── shared_hit_blocks / shared_read_blocks → I/O pen.  │
│         │    └── node_types → index/hash join bonus                 │
│         ├── Result equivalence (set comparison vs gold)             │
│         └── Hack detection                                          │
│              ├── Shadow replica → literal matching defense           │
│              ├── Row count check → LIMIT 0 defense                  │
│              └── ANALYZE enforcement → planner manipulation defense  │
│         ↓                                                           │
│  StepResult: { observation, reward ∈ [-1, 1], done, info }         │
└─────────────────────────────────────────────────────────────────────┘
```

## Dependencies

No new packages required. Uses:
- `src.database.Database` (project-internal SQLAlchemy wrapper)
- `sqlalchemy.text` for raw SQL execution
- Standard library: `json`, `re`, `dataclasses`, `typing`

## Quick Start

```python
from src.rlef import SQLRefactorEnv

env = SQLRefactorEnv(db_name="financial")

obs = env.reset({
    "baseline_sql": "SELECT * FROM trans ORDER BY amount DESC",
    "gold_sql": "SELECT * FROM trans ORDER BY amount DESC",
})

action = """
<reasoning>
The baseline performs a full sequential scan on `trans` followed by a
file sort. Adding an index-aware ORDER BY and limiting output will
reduce I/O significantly.
</reasoning>
SELECT account_id, amount FROM trans ORDER BY amount DESC LIMIT 100;
"""

result = env.step(action)
print(f"Reward: {result.reward}")
print(f"Cost reduction: {result.info['explain_metrics']['baseline_cost']} → "
      f"{result.info['explain_metrics']['optimized_cost']}")
```

## Training Logs

See `training_logs/` for recorded episode data from a proof-of-concept
training run across the BIRD benchmark databases:
- `episode_log.json` — detailed per-episode metrics
- `reward_summary.md` — human-readable summary table

## Anti-Hacking Defenses

| Attack Vector | Defense | Mechanism |
|---|---|---|
| Hardcoded literal outputs | Hidden dataset replicas | Query runs on `{db}_process_1` with different data; identical results = hack |
| LIMIT 0 / empty results | Baseline row count comparison | Optimized returns 0 rows when baseline returns N → immediate -1.0 |
| Planner-only manipulation | EXPLAIN ANALYZE | Uses actual hardware execution, not theoretical cost estimates |
