"""Request-local, content-free diagnostics for the three segment commands."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time

CURRENT = ContextVar("segment_timing", default=None)


class ReadTiming:
    def __init__(self):
        self.started = time.perf_counter()
        self.cpu_clock = getattr(time, "thread_time", time.process_time)
        self.cpu_started = self.cpu_clock()
        self.started_at = int(time.time() * 1000)
        self.phases = {}
        self.calls = {}
        self.counts = {}
        self.host_read_ms = None
        self.import_ms = None

    @contextmanager
    def span(self, name):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0) + (time.perf_counter() - started) * 1000
            self.calls[name] = self.calls.get(name, 0) + 1

    def snapshot(self):
        result = {
            "version": 1,
            "startedAtUnixMs": self.started_at,
            "measuredAtUnixMs": int(time.time() * 1000),
            "hostElapsedMs": round((time.perf_counter() - self.started) * 1000, 3),
            "hostCpuMs": round((self.cpu_clock() - self.cpu_started) * 1000, 3),
            "phasesMs": {key: round(value, 3) for key, value in self.phases.items()},
            "phaseCalls": dict(self.calls),
            "counts": dict(self.counts),
        }
        if self.host_read_ms is not None:
            result["hostReadMs"] = round(self.host_read_ms, 3)
        if self.import_ms is not None:
            result["cliModuleImportMs"] = round(self.import_ms, 3)
        return result


def count(name, value=1):
    timing = CURRENT.get()
    if timing is not None:
        timing.counts[name] = timing.counts.get(name, 0) + value


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            timing = CURRENT.get()
            if timing is None:
                return function(*args, **kwargs)
            with timing.span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def measured_read(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        timing = CURRENT.get() or ReadTiming()
        token = CURRENT.set(timing)
        started = time.perf_counter()
        try:
            result = function(*args, **kwargs)
            timing.host_read_ms = (time.perf_counter() - started) * 1000
            result["timings"] = timing.snapshot()
            return result
        except Exception as error:
            timing.host_read_ms = (time.perf_counter() - started) * 1000
            error.segment_timings = timing.snapshot()
            raise
        finally:
            CURRENT.reset(token)
    return wrapped
