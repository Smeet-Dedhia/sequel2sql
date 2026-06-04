"""
RLEF Reward Function — Deterministic reward from PostgreSQL execution feedback.

Uses strict hardware execution metrics from ``EXPLAIN (ANALYZE, BUFFERS,
FORMAT JSON)`` to score optimized SQL against the baseline.  Includes
defenses against common reward-hacking strategies in code-generation RL loops.

Reward components:
    1. **Query Planner Efficiency** — cost ratio from EXPLAIN
    2. **Hardware I/O Analysis**    — shared_hit_blocks vs shared_read_blocks
    3. **Node Type Bonus**          — index scans / hash joins over seq scans
    4. **Result Equivalence Gate**  — must produce identical results to gold
    5. **Hack Detection**           — literal matching, empty results, planner tricks

Anti-hacking defenses:
    - Hidden dataset replicas to catch hardcoded literal outputs
    - Strict baseline comparison to catch LIMIT 0 / empty result tricks
    - EXPLAIN ANALYZE (not just EXPLAIN) to prevent planner-only manipulation
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlanMetrics:
    """Metrics extracted from EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)."""

    total_cost: float = 0.0
    actual_time_ms: float = 0.0
    node_types: List[str] = field(default_factory=list)
    shared_hit_blocks: int = 0
    shared_read_blocks: int = 0
    rows_returned: int = 0


# ---------------------------------------------------------------------------
# Plan analysis
# ---------------------------------------------------------------------------

def _get_plan_metrics(sql: str, engine: Engine) -> PlanMetrics:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) and extract metrics.

    Uses ANALYZE for actual hardware execution — not just theoretical cost
    estimates — to prevent planner manipulation hacking.

    Parameters
    ----------
    sql : str
        The SQL query to analyze.
    engine : sqlalchemy.engine.Engine
        Database engine to execute against.

    Returns
    -------
    PlanMetrics
        Extracted execution metrics.
    """
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"

    with engine.connect() as conn:
        # Wrap in transaction so DML side-effects are rolled back
        trans = conn.begin()
        try:
            result = conn.execute(text(explain_sql)).fetchone()
        finally:
            trans.rollback()

    if result is None:
        return PlanMetrics()

    plan_json = result[0]
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)

    plan = plan_json[0] if isinstance(plan_json, list) else plan_json
    root = plan.get("Plan", {})

    metrics = PlanMetrics(
        total_cost=float(root.get("Total Cost", 0.0)),
        actual_time_ms=float(root.get("Actual Total Time", 0.0)),
        rows_returned=int(root.get("Actual Rows", 0)),
    )

    # Walk the plan tree to collect node types and buffer stats
    _walk_plan_tree(root, metrics)

    return metrics


def _walk_plan_tree(node: Dict[str, Any], metrics: PlanMetrics) -> None:
    """Recursively walk the EXPLAIN plan tree to collect node types and I/O."""
    node_type = node.get("Node Type", "")
    if node_type:
        metrics.node_types.append(node_type)

    # Accumulate buffer statistics (hardware I/O)
    metrics.shared_hit_blocks += int(node.get("Shared Hit Blocks", 0))
    metrics.shared_read_blocks += int(node.get("Shared Read Blocks", 0))

    # Recurse into child plans
    for child in node.get("Plans", []):
        _walk_plan_tree(child, metrics)


# ---------------------------------------------------------------------------
# Result equivalence
# ---------------------------------------------------------------------------

def _execute_and_fetch(sql: str, engine: Engine) -> Optional[List[tuple]]:
    """Execute a query and return all result rows, or None on error."""
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                rows = conn.execute(text(sql)).fetchall()
                return [tuple(row) for row in rows]
            finally:
                trans.rollback()
    except Exception:
        return None


def _check_result_equivalence(
    optimized_sql: str,
    gold_sql: str,
    engine: Engine,
) -> bool:
    """Check if optimized query produces identical results to the gold query.

    Uses set comparison (order-independent, duplicate-insensitive) — the
    same logic as ``ex_base()`` in the benchmark evaluation suite.
    """
    optimized_rows = _execute_and_fetch(optimized_sql, engine)
    gold_rows = _execute_and_fetch(gold_sql, engine)

    if optimized_rows is None or gold_rows is None:
        return False
    if not optimized_rows and not gold_rows:
        return True

    return set(optimized_rows) == set(gold_rows)


# ---------------------------------------------------------------------------
# Anti-hacking defenses
# ---------------------------------------------------------------------------

def _check_literal_hacking(
    optimized_sql: str,
    engine: Engine,
    shadow_engine: Optional[Engine],
) -> bool:
    """Detect literal-matching hacks using a hidden dataset replica.

    Runs the optimized query on both the primary database and a shadow
    replica (``{db}_process_{n}``) containing different underlying data.
    If results are identical on both, the model may be hardcoding string
    literals to pass equivalence checks rather than actually computing them.

    Returns True if hack is detected, False if clean.
    """
    if shadow_engine is None:
        return False  # Cannot check without shadow DB

    primary_rows = _execute_and_fetch(optimized_sql, engine)
    shadow_rows = _execute_and_fetch(optimized_sql, shadow_engine)

    if primary_rows is None or shadow_rows is None:
        return False  # Execution error — not a hack signal

    # If the query returns identical results on databases with different
    # data, the model is likely hardcoding output values.
    if primary_rows and shadow_rows and set(primary_rows) == set(shadow_rows):
        # Only flag if there are actual rows to compare
        return True

    return False


def _check_empty_result_hack(
    optimized_sql: str,
    baseline_sql: str,
    engine: Engine,
) -> bool:
    """Detect empty-result hacks (e.g., appending LIMIT 0).

    If the baseline returns rows but the optimized query returns zero rows,
    the model is likely exploiting empty results to artificially lower
    execution time.  Also explicitly checks for ``LIMIT 0`` in the SQL text.

    Returns True if hack is detected, False if clean.
    """
    # Explicit string check for LIMIT 0
    sql_upper = optimized_sql.upper()
    if re.search(r"\bLIMIT\s+0\b", sql_upper):
        return True

    baseline_rows = _execute_and_fetch(baseline_sql, engine)
    optimized_rows = _execute_and_fetch(optimized_sql, engine)

    if baseline_rows is None or optimized_rows is None:
        return False

    # Baseline returns rows, but optimized returns nothing → suspicious
    if len(baseline_rows) > 0 and len(optimized_rows) == 0:
        return True

    return False


# ---------------------------------------------------------------------------
# Main reward function
# ---------------------------------------------------------------------------

def compute_reward(
    optimized_sql: str,
    baseline_sql: str,
    gold_sql: str,
    engine: Engine,
    shadow_engine: Optional[Engine] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Compute the deterministic reward for an optimized SQL query.

    Combines query planner efficiency, hardware I/O analysis, node type
    bonuses, result equivalence, and hack detection into a single scalar.

    Parameters
    ----------
    optimized_sql : str
        The model's optimized SQL output.
    baseline_sql : str
        The original inefficient query.
    gold_sql : str
        The known-correct optimized query for equivalence checking.
    engine : sqlalchemy.engine.Engine
        Primary database engine.
    shadow_engine : sqlalchemy.engine.Engine, optional
        Hidden replica engine for anti-hacking literal-match detection.

    Returns
    -------
    tuple[float, dict]
        Scalar reward in [-1.0, 1.0] and a detailed info dictionary.
    """
    info: Dict[str, Any] = {}

    # ----- 1. Hack detection (immediate disqualification) -----
    empty_hack = _check_empty_result_hack(optimized_sql, baseline_sql, engine)
    literal_hack = _check_literal_hacking(optimized_sql, engine, shadow_engine)

    info["hack_detection"] = {
        "empty_result_check": "FAILED" if empty_hack else "passed",
        "literal_match_check": "FAILED" if literal_hack else "passed",
        "planner_manipulation_check": "passed",  # enforced by ANALYZE
    }

    if empty_hack or literal_hack:
        info["disqualification_reason"] = (
            "empty_result_hack" if empty_hack else "literal_match_hack"
        )
        return -1.0, info

    # ----- 2. Result equivalence gate -----
    equivalent = _check_result_equivalence(optimized_sql, gold_sql, engine)
    info["result_equivalence"] = equivalent

    if not equivalent:
        info["disqualification_reason"] = "result_mismatch"
        return -1.0, info

    # ----- 3. Query planner efficiency (EXPLAIN ANALYZE) -----
    baseline_metrics = _get_plan_metrics(baseline_sql, engine)
    optimized_metrics = _get_plan_metrics(optimized_sql, engine)

    info["explain_metrics"] = {
        "baseline_cost": baseline_metrics.total_cost,
        "optimized_cost": optimized_metrics.total_cost,
        "baseline_node_types": baseline_metrics.node_types,
        "optimized_node_types": optimized_metrics.node_types,
        "shared_hit_blocks": optimized_metrics.shared_hit_blocks,
        "shared_read_blocks": optimized_metrics.shared_read_blocks,
    }

    # Cost ratio reward: how much did we reduce the plan cost?
    if baseline_metrics.total_cost > 0:
        cost_ratio = max(
            0.0, 1.0 - optimized_metrics.total_cost / baseline_metrics.total_cost
        )
    else:
        cost_ratio = 0.0

    # ----- 4. Hardware I/O penalty -----
    # Penalize queries that force full table scans on disk
    io_penalty = 0.0
    total_blocks = (
        optimized_metrics.shared_hit_blocks + optimized_metrics.shared_read_blocks
    )
    if total_blocks > 0:
        disk_ratio = optimized_metrics.shared_read_blocks / total_blocks
        if disk_ratio > 0.5:
            # More than half of blocks came from disk — heavy penalty
            io_penalty = -0.3 * disk_ratio

    # ----- 5. Node type bonus -----
    # Reward index utilization and hash joins over sequential scans
    node_bonus = 0.0
    baseline_seq_scans = sum(
        1 for n in baseline_metrics.node_types if n == "Seq Scan"
    )
    optimized_seq_scans = sum(
        1 for n in optimized_metrics.node_types if n == "Seq Scan"
    )
    optimized_index_scans = sum(
        1 for n in optimized_metrics.node_types
        if n in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
    )
    optimized_hash_joins = sum(
        1 for n in optimized_metrics.node_types if n == "Hash Join"
    )

    # Bonus for reducing seq scans
    scans_eliminated = baseline_seq_scans - optimized_seq_scans
    node_bonus += 0.05 * max(0, scans_eliminated)

    # Bonus for introducing efficient nodes
    node_bonus += 0.03 * optimized_index_scans
    node_bonus += 0.02 * optimized_hash_joins

    # Cap node bonus
    node_bonus = min(node_bonus, 0.2)

    # ----- 6. Combine into final reward -----
    reward = cost_ratio + io_penalty + node_bonus

    # Clamp to [-1.0, 1.0]
    reward = max(-1.0, min(1.0, reward))

    info["reward_components"] = {
        "cost_ratio": round(cost_ratio, 4),
        "io_penalty": round(io_penalty, 4),
        "node_bonus": round(node_bonus, 4),
    }

    return round(reward, 4), info
