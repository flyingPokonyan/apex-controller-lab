"""Bounded timing summaries; collecting diagnostics never changes workflow decisions."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
import time


class PerformanceMetrics:
    def __init__(self, log, *, clock=time.monotonic, interval_s=30.0):
        self.log = log
        self.clock = clock
        self.interval_s = interval_s
        self.started = clock()
        self.samples = defaultdict(lambda: deque(maxlen=256))
        self.totals = defaultdict(lambda: [0, 0.0, 0.0])

    def add(self, name, milliseconds):
        value = max(0.0, float(milliseconds))
        self.samples[name].append(value)
        total = self.totals[name]
        total[0] += 1
        total[1] += value
        total[2] = max(total[2], value)

    @contextmanager
    def measure(self, name):
        start = self.clock()
        try:
            yield
        finally:
            self.add(name, (self.clock() - start) * 1000)

    def flush(self, *, force=False, **context):
        now = self.clock()
        if not self.totals or (not force and now - self.started < self.interval_s):
            return
        metrics = {}
        for name, (count, total, maximum) in self.totals.items():
            ordered = sorted(self.samples[name])
            metrics[name] = {
                "count": count, "totalMs": round(total, 2),
                "meanMs": round(total / count, 2), "maxMs": round(maximum, 2),
                "recentP95Ms": round(ordered[int((len(ordered) - 1) * .95)], 2),
            }
        try:
            self.log("PERFORMANCE_SUMMARY", windowMs=round((now - self.started) * 1000),
                     metrics=metrics, **context)
        except Exception:
            pass
        self.samples.clear()
        self.totals.clear()
        self.started = now
