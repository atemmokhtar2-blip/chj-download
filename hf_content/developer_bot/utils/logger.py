import logging
import os

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_logs = os.path.join(_root, "logs")
os.makedirs(_logs, exist_ok=True)


def _make(name: str, filename: str, level=logging.INFO) -> logging.Logger:
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(os.path.join(_logs, filename), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log = logging.getLogger(name)
    log.setLevel(level)
    if not log.handlers:
        log.addHandler(fh)
        log.addHandler(sh)
    return log


system_logger = _make("dev.system",  "dev_system.log")
action_logger = _make("dev.actions", "dev_actions.log")
error_logger  = _make("dev.errors",  "dev_errors.log", logging.ERROR)
