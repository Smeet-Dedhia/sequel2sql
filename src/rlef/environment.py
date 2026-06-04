"""
RLEF Environment — Gym-compatible RL environment for SQL refactoring.

Uses a live PostgreSQL engine to provide deterministic observations and
rewards.  The agent observes the full normalized schema, realistic database
statistics, and a computationally inefficient baseline query, then produces
a two-step action: an explicit reasoning trace followed by optimized SQL.

Designed for *Algorithmic Refactoring* — teaching models to exploit query
planner mechanics (index utilization, hash joins, buffer management)
rather than relying on biased LLM-as-a-Judge evaluations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from src.database import Database
from src.rlef.reward import compute_reward


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """Full observation presented to the policy at each timestep."""

    schema: str
    """DDL-like schema description from Database.describe_schema()."""

    db_stats: List[Dict[str, Any]]
    """Row-level statistics from pg_stat_user_tables (row counts, etc.)."""

    baseline_sql: str
    """The computationally inefficient baseline query to be refactored."""

    baseline_cost: float
    """Total plan cost of the baseline query from EXPLAIN (FORMAT JSON)."""

    baseline_plan: Dict[str, Any]
    """Full EXPLAIN output for the baseline query."""


@dataclass
class StepResult:
    """Result returned by SQLRefactorEnv.step()."""

    observation: Observation
    reward: float
    done: bool
    info: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class SQLRefactorEnv:
    """Deterministic RL environment for SQL algorithmic refactoring.

    Wraps a live PostgreSQL database and uses strict hardware execution
    metrics — not LLM-as-a-Judge — to score generated SQL.

    Observation space (dict):
        - schema:        Full normalized DDL-like schema text
        - db_stats:      Realistic database statistics (row counts, estimates)
        - baseline_sql:  The inefficient query to optimize
        - baseline_cost: Execution plan cost of the baseline

    Action space (str):
        A two-step generation string enforcing an explicit reasoning trace
        of query planner constraints, followed by the optimized SQL:

            <reasoning>
            The baseline uses a sequential scan on the `trans` table ...
            Switching to an indexed lookup on account_id will reduce I/O ...
            </reasoning>
            SELECT a.account_id, SUM(t.amount) AS total
            FROM account a
            JOIN trans t USING (account_id)
            GROUP BY a.account_id
            ORDER BY total DESC;

    Reward signal:
        Deterministic, computed from EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON).
        See reward.py for the full reward function.

    Parameters
    ----------
    db_name : str
        Name of the PostgreSQL database to connect to.
    shadow_db_name : str, optional
        Name of a hidden replica database for anti-hacking checks.
        Follows the existing ``{db}_process_{n}`` naming convention.
    host : str
        PostgreSQL host. Default ``localhost``.
    port : int
        PostgreSQL port. Default ``5432``.
    user : str
        PostgreSQL user. Default ``root``.
    password : str
        PostgreSQL password. Default ``123123``.
    """

    def __init__(
        self,
        db_name: str,
        shadow_db_name: Optional[str] = None,
        host: str = "localhost",
        port: int = 5432,
        user: str = "root",
        password: str = "123123",
    ) -> None:
        self.db_name = db_name
        self.shadow_db_name = shadow_db_name or f"{db_name}_process_1"
        self._host = host
        self._port = port
        self._user = user
        self._password = password

        # Connections are initialized lazily in reset()
        self.db: Optional[Database] = None
        self.shadow_db: Optional[Database] = None

        # Current episode state
        self._observation: Optional[Observation] = None
        self._baseline_sql: Optional[str] = None
        self._gold_sql: Optional[str] = None
        self._done: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self, instance: Dict[str, Any]) -> Observation:
        """Begin a new episode.

        Parameters
        ----------
        instance : dict
            Must contain:
            - ``baseline_sql`` : str — the inefficient query
            - ``gold_sql``     : str — the known-correct optimized query
            Optionally:
            - ``db_name``      : str — override the database for this episode

        Returns
        -------
        Observation
            The initial observation for the policy.
        """
        db_name = instance.get("db_name", self.db_name)

        # (Re-)initialize database connections
        self.db = Database(
            database_name=db_name,
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
        )
        self.shadow_db = Database(
            database_name=instance.get("shadow_db_name", self.shadow_db_name),
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
        )

        self._baseline_sql = instance["baseline_sql"]
        self._gold_sql = instance["gold_sql"]
        self._done = False

        self._observation = self._build_observation()
        return self._observation

    def step(self, action: str) -> StepResult:
        """Execute one environment step.

        Parameters
        ----------
        action : str
            Two-part string: ``<reasoning>...</reasoning>`` followed by
            the optimized SQL query.

        Returns
        -------
        StepResult
            Contains the (unchanged) observation, scalar reward, done flag,
            and an info dict with detailed EXPLAIN metrics and hack flags.
        """
        if self._done:
            raise RuntimeError("Episode is done. Call reset() first.")

        assert self.db is not None
        assert self._observation is not None
        assert self._baseline_sql is not None
        assert self._gold_sql is not None

        # Parse the two-step action
        reasoning_trace, optimized_sql = self._parse_action(action)

        # Compute deterministic reward via EXPLAIN ANALYZE
        reward_value, reward_info = compute_reward(
            optimized_sql=optimized_sql,
            baseline_sql=self._baseline_sql,
            gold_sql=self._gold_sql,
            engine=self.db.engine,
            shadow_engine=self.shadow_db.engine if self.shadow_db else None,
        )

        self._done = True

        info = {
            "reasoning_trace": reasoning_trace,
            "optimized_sql": optimized_sql,
            **reward_info,
        }

        return StepResult(
            observation=self._observation,
            reward=reward_value,
            done=True,
            info=info,
        )

    def render(self) -> str:
        """Return a human-readable summary of the current state."""
        if self._observation is None:
            return "Environment not initialized. Call reset()."
        return (
            f"DB: {self.db_name}\n"
            f"Baseline cost: {self._observation.baseline_cost:.2f}\n"
            f"Baseline SQL:\n{self._observation.baseline_sql}\n"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_observation(self) -> Observation:
        """Construct the full observation dict for the policy."""
        assert self.db is not None
        assert self._baseline_sql is not None

        # 1. Full normalized schema
        schema = self.db.describe_schema()

        # 2. Database statistics (row counts, value estimates)
        db_stats = self._get_db_stats()

        # 3. Baseline plan cost via EXPLAIN (FORMAT JSON)
        baseline_plan, baseline_cost = self._get_plan_cost(self._baseline_sql)

        return Observation(
            schema=schema,
            db_stats=db_stats,
            baseline_sql=self._baseline_sql,
            baseline_cost=baseline_cost,
            baseline_plan=baseline_plan,
        )

    def _get_db_stats(self) -> List[Dict[str, Any]]:
        """Retrieve realistic database statistics from pg_stat_user_tables."""
        assert self.db is not None
        stats_sql = (
            "SELECT relname, n_live_tup, n_dead_tup, "
            "seq_scan, idx_scan, seq_tup_read, idx_tup_fetch "
            "FROM pg_stat_user_tables ORDER BY n_live_tup DESC"
        )
        with self.db.engine.connect() as conn:
            rows = conn.execute(text(stats_sql)).fetchall()
        return [
            {
                "table": row[0],
                "live_rows": row[1],
                "dead_rows": row[2],
                "seq_scans": row[3],
                "idx_scans": row[4],
                "seq_tup_read": row[5],
                "idx_tup_fetch": row[6],
            }
            for row in rows
        ]

    def _get_plan_cost(self, sql: str) -> Tuple[Dict[str, Any], float]:
        """Run EXPLAIN (FORMAT JSON) and extract the total plan cost."""
        explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
        with self.db.engine.connect() as conn:
            result = conn.execute(text(explain_sql)).fetchone()

        if result is None:
            return {}, 0.0

        plan_json = result[0]
        if isinstance(plan_json, str):
            plan_json = json.loads(plan_json)

        plan = plan_json[0] if isinstance(plan_json, list) else plan_json
        total_cost = float(plan.get("Plan", {}).get("Total Cost", 0.0))
        return plan, total_cost

    @staticmethod
    def _parse_action(action: str) -> Tuple[str, str]:
        """Split a two-step action into reasoning trace and SQL.

        Expected format:
            <reasoning>...</reasoning>
            SQL HERE
        """
        match = re.search(
            r"<reasoning>(.*?)</reasoning>\s*(.*)",
            action,
            re.DOTALL,
        )
        if match:
            return match.group(1).strip(), match.group(2).strip()

        # Fallback: no reasoning block — treat entire action as SQL
        return "", action.strip()
