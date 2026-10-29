"""Offline evaluation utilities — precision/recall/F1 vs ground truth.

Directive §10/§20: engines must be measured against baselines and labeled
fixtures before any reliability claim.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvalResult:
    label: str
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {"label": self.label, "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "precision": round(self.precision, 3), "recall": round(self.recall, 3),
                "f1": round(self.f1, 3)}


def binary_eval(label: str, truth: set[str], predicted: set[str], universe: set[str] | None = None) -> EvalResult:
    tp = len(truth & predicted)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    tn = len((universe or (truth | predicted)) - truth - predicted) if universe else 0
    return EvalResult(label, tp, fp, fn, tn)
