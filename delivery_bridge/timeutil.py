"""时间戳格式。落盘和接口一律用同一种 UTC 写法。"""

from __future__ import annotations

from datetime import datetime, timezone

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
