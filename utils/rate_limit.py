"""Rate limiting utilities."""
import time

_rate_limits: dict = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10


def check_rate_limit(key: str) -> bool:
    now = time.time()
    if key not in _rate_limits:
        _rate_limits[key] = [1, now]
        return True
    count, start = _rate_limits[key]
    if now - start > RATE_LIMIT_WINDOW:
        _rate_limits[key] = [1, now]
        return True
    if count >= RATE_LIMIT_MAX:
        return False
    _rate_limits[key][0] += 1
    return True
