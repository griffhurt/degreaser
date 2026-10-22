"""Human-readable formatting shared by windows and widgets."""

import time


def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def human_count(n: int) -> str:
    return f"{n:,}"


def relative_time(ts: float | None, now: float | None = None) -> str:
    if ts is None:
        return "never"
    delta = (now or time.time()) - ts
    if delta < 60:
        return "just now"
    for seconds, unit in ((86400 * 30, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if delta >= seconds:
            n = int(delta // seconds)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
