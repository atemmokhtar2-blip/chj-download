from __future__ import annotations

_STATE = {"enabled": False}


def is_maintenance_mode() -> bool:
    return bool(_STATE["enabled"])


def set_maintenance_mode(enabled: bool) -> None:
    _STATE["enabled"] = bool(enabled)
