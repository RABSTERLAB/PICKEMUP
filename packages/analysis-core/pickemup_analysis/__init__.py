"""PICKEMUP analysis core — explainable intelligence engines.

Design rules:
- Every result carries evidence references, an algorithm version, a confidence,
  and an explicit INSUFFICIENT_EVIDENCE state when data is too sparse.
- No hidden ML: MVP engines are deterministic, pure-Python signal composites.
- Correlation is never reported as proven coordination or authorship.
"""

ALGO_VERSION = "2026.10-mvp2"

# Verdict vocabulary shared across engines
VERDICT_LIKELY_AUTOMATED = "likely_automated"
VERDICT_MIXED_AUTOMATION = "mixed_automation"  # automation-assisted over human base
VERDICT_LIKELY_HUMAN = "likely_human"
VERDICT_HUMAN_COORDINATED_POSSIBLE = "human_coordinated_possible"
VERDICT_INSUFFICIENT = "insufficient_evidence"
VERDICT_INDETERMINATE = "indeterminate"

__all__ = [
    "ALGO_VERSION",
    "VERDICT_LIKELY_AUTOMATED",
    "VERDICT_MIXED_AUTOMATION",
    "VERDICT_LIKELY_HUMAN",
    "VERDICT_HUMAN_COORDINATED_POSSIBLE",
    "VERDICT_INSUFFICIENT",
    "VERDICT_INDETERMINATE",
]
