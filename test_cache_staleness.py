"""Cache staleness ceiling — the bug that served a 7-hour-old pass list.

Socket-free and radio-free: these drive server._cached() directly with a
counting function and a fake clock. No Flask request, no engine, no TLE.

The defect: stale-while-revalidate with no ceiling served whatever was last
computed, however old, because nothing polls this service between passes. The
first visitor -- the one who matters -- got the stale answer, and only the
second got a correct one.
"""
import sys
import threading
import time

import server

failures = 0


def check(cond, label):
    global failures
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures += 1


def reset():
    with server._lock:
        server._cache.clear()
        server._refreshing.clear()


def settle(deadline=3.0):
    """Wait for background refresh threads to finish, rather than sleeping blind."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        with server._lock:
            if not server._refreshing:
                return True
        time.sleep(0.01)
    return False


class Clock:
    """Replaces time.monotonic inside server so ages are exact, not timing-dependent."""

    def __init__(self):
        self.t = 1000.0
        self._real = server.time.monotonic

    def __enter__(self):
        server.time.monotonic = lambda: self.t
        return self

    def __exit__(self, *a):
        server.time.monotonic = self._real

    def advance(self, dt):
        self.t += dt


print("cache staleness")

# --- 1. fresh values are served from cache, not recomputed -----------------
reset()
calls = []
with Clock() as clk:
    for _ in range(3):
        server._cached("k", 60, lambda: calls.append(1) or "v1", max_stale=300)
    check(len(calls) == 1, "inside the TTL the value is served from cache")

    # --- 2. expired but INSIDE the ceiling: stale served, refresh scheduled -
    clk.advance(120)          # age 120: past ttl=60, inside max_stale=300
    got = server._cached("k", 60, lambda: calls.append(1) or "v2", max_stale=300)
    check(got == "v1", "past the TTL but inside the ceiling, the stale value is still served")
settle()
check(len(calls) == 2, "...and a background refresh was started to replace it")

# --- 3. past the ceiling: the caller waits for a correct value -------------
# THE REGRESSION. Before the fix this returned the stale value no matter how old.
reset()
calls = []
with Clock() as clk:
    server._cached("k", 60, lambda: calls.append(1) or "old", max_stale=300)
    clk.advance(7 * 3600)     # the observed failure: seven hours
    got = server._cached("k", 60, lambda: calls.append(1) or "fresh", max_stale=300)
    check(got == "fresh", "past the ceiling a seven-hour-old value is NOT served")
    check(got != "old", "...the caller is not handed yesterday's passes")
settle()

# --- 4. max_stale=None keeps the old unbounded behaviour -------------------
reset()
with Clock() as clk:
    server._cached("k", 60, lambda: "old")
    clk.advance(7 * 3600)
    got = server._cached("k", 60, lambda: "fresh")
    check(got == "old", "max_stale=None still serves unbounded stale (opt-in ceiling)")
settle()

# --- 5. a failing background refresh must EVICT, not serve forever ---------
# Previously the exception was logged and the stale entry stayed, with no
# ceiling and nothing to replace it: one transient failure became permanent.
reset()
with Clock() as clk:
    server._cached("k", 60, lambda: "good", max_stale=300)
    clk.advance(120)

    def boom():
        raise RuntimeError("refresh failed")

    server._cached("k", 60, boom, max_stale=300)   # serves stale, refresh raises
    settle()
    with server._lock:
        evicted = "k" not in server._cache
    check(evicted, "a background refresh that raises evicts the entry")
    got = server._cached("k", 60, lambda: "recovered", max_stale=300)
    check(got == "recovered", "...so the next caller recomputes instead of being stuck")

# --- 6. only one refresh thread per key under concurrency ------------------
reset()
started = []
with Clock() as clk:
    server._cached("k", 60, lambda: "v", max_stale=300)
    clk.advance(120)

    def slow():
        started.append(1)
        time.sleep(0.05)
        return "v2"

    threads = [threading.Thread(target=lambda: server._cached("k", 60, slow, max_stale=300))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
settle()
check(len(started) == 1, "concurrent callers start exactly one refresh, not eight")

# --- 7. the real constants are sane ---------------------------------------
check(server.PASS_MAX_STALE_S > server.PASS_TTL_S,
      "the pass ceiling is longer than its refresh interval")
check(server.LIVE_MAX_STALE_S > server.LIVE_TTL_S,
      "the live ceiling is longer than its refresh interval")
check(server.PASS_MAX_STALE_S <= 900,
      "the pass ceiling is short enough that the countdown is still usable")

print("test_cache_staleness: %d failure(s)" % failures)
sys.exit(1 if failures else 0)
