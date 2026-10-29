"""PICKEMUP connector framework.

Every connector declares identity, capabilities, auth, rate limits, costs,
coverage gaps and retention rules (directive §8), and returns normalized
records + provenance. `origin_mode` ('live' | 'mock' | 'synthetic' | 'fixture')
is stamped on every record so demo data can never masquerade as real.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pickemup_analysis.domain import AccountObs, PostObs


@dataclass
class ConnectorManifest:
    name: str
    platform: str
    capabilities: list[str]              # search, account_lookup, import, generate
    auth_required: bool
    rate_limit_desc: str
    cost_model: str                      # free | usd_per_read:0.005+... | n/a
    coverage_gaps: list[str]
    retention_rule: str
    data_freshness: str


@dataclass
class IngestBatch:
    accounts: list[AccountObs] = field(default_factory=list)
    posts: list[PostObs] = field(default_factory=list)
    origin_mode: str = "live"
    provenance: dict = field(default_factory=dict)   # source_kind, job_id, collected_at, ...


@dataclass
class CostEstimate:
    post_reads: int = 0
    user_reads: int = 0
    usd: float = 0.0
    capped_by: str | None = None         # job_budget | daily_budget | None


class BudgetGuard:
    """Per-job + per-day spend caps. Refuses collection beyond limits."""

    def __init__(self, per_job_usd: float, per_day_usd: float,
                 post_read_usd: float, user_read_usd: float):
        self.per_job = per_job_usd
        self.per_day = per_day_usd
        self.post_read = post_read_usd
        self.user_read = user_read_usd
        self.job_spent = 0.0
        self.day_spent = 0.0

    def estimate(self, post_reads: int, user_reads: int = 0) -> CostEstimate:
        # negative or fractional counts must never reduce spend (cap bypass)
        post_reads = max(0, int(post_reads))
        user_reads = max(0, int(user_reads))
        usd = post_reads * self.post_read + user_reads * self.user_read
        est = CostEstimate(post_reads, user_reads, round(usd, 6))
        if self.job_spent + usd > self.per_job:
            est.capped_by = "job_budget"
        elif self.day_spent + usd > self.per_day:
            est.capped_by = "daily_budget"
        return est

    def charge(self, est: CostEstimate) -> None:
        if est.usd < 0:
            raise BudgetExceeded("cost estimate must never be negative")
        if est.capped_by:
            raise BudgetExceeded(f"collection refused: {est.capped_by} reached "
                                 f"(job=${self.job_spent:.4f}/${self.per_job}, "
                                 f"day=${self.day_spent:.4f}/${self.per_day})")
        self.job_spent += est.usd
        self.day_spent += est.usd


class BudgetExceeded(RuntimeError):
    pass


class Connector(ABC):
    manifest: ConnectorManifest
    # True = local dev/QA surface (synthetic data, mocks, file importers).
    # Production consoles must not list or run these.
    dev_only: bool = False

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(is_available, reason). Offline/mock fallbacks must announce themselves."""

    @abstractmethod
    def run(self, params: dict, budget: BudgetGuard | None = None) -> IngestBatch:
        ...

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
