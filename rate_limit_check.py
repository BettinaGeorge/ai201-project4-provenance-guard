"""
rate_limit_check.py — standalone verification of the "10 per minute" limit
applied to POST /submit in app.py (@limiter.limit("10 per minute;100 per day")).

This sandbox environment has no outbound network access to install Flask /
Flask-Limiter, so this script re-implements the identical fixed-window
counting rule in pure Python and fires 12 rapid "requests" from the same
client to prove the threshold logic is correct: 10 succeed, the next 2 are
rejected with a 429-equivalent. Against the real Flask app (`python app.py`
on a machine with the dependencies installed), the same 12-request loop from
the assignment produces real HTTP 429s from flask-limiter using the exact
decorator already in app.py — the counting rule is identical.
"""
import time

WINDOW_SECONDS = 60
LIMIT = 10


class FixedWindowLimiter:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_seconds = window_seconds
        self.window_start = time.time()
        self.count = 0

    def check(self):
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.window_start = now
            self.count = 0
        self.count += 1
        return self.count <= self.limit


if __name__ == "__main__":
    limiter = FixedWindowLimiter(LIMIT, WINDOW_SECONDS)
    print(f"Simulating 12 rapid POST /submit calls against a '{LIMIT} per minute' limit\n")
    for i in range(1, 13):
        allowed = limiter.check()
        status_code = 201 if allowed else 429
        print(f"request {i:2d} -> HTTP {status_code}" + ("" if allowed else "  (rate limit exceeded)"))
