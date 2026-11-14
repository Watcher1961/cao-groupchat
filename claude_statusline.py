#!/usr/bin/env python3
"""Silent statusLine sink: persist only quota percentages, reset times and age."""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time


def capture() -> None:
    raw = sys.stdin.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    limits = payload.get("rate_limits")
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        return
    now = time.time()
    filtered = {}
    for name in ("five_hour", "seven_day", "spend_limit"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used, resets = window.get("used_percentage"), window.get("resets_at")
        if (type(used) not in (int, float) or type(resets) not in (int, float)
                or not math.isfinite(used) or not math.isfinite(resets)
                or not 0 <= used <= 100 or resets <= now):
            continue
        filtered[name] = {"used_percentage": used, "resets_at": resets}
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".claude-limits-", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"updated_at": now, "rate_limits": filtered}, output, allow_nan=False)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        capture()
    except BaseException:
        # A status bar must never disrupt the CLI, including on bad input/disk.
        pass
