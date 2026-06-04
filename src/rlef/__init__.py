"""
Reinforcement Learning from Execution Feedback (RLEF) module.

Provides a Gym-compatible RL environment and deterministic reward
computation for training language models to perform SQL algorithmic
refactoring against a live PostgreSQL engine.

Architecture overview:
    - environment.py : SQLRefactorEnv — observation/action/step loop
    - reward.py      : Deterministic reward from EXPLAIN ANALYZE metrics
    - training_logs/ : Recorded episode data from training runs
"""

from .environment import SQLRefactorEnv
from .reward import compute_reward

__all__ = ["SQLRefactorEnv", "compute_reward"]
