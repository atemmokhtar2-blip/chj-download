"""Entry point for TitanX Control Panel."""
import sys
import os

# Force .pythonlibs FIRST — must happen before any other imports
_pythonlibs = "/home/runner/workspace/.pythonlibs/lib/python3.12/site-packages"
if _pythonlibs not in sys.path:
    sys.path.insert(0, _pythonlibs)

# Ensure extracted_project is importable
_here = os.path.dirname(os.path.abspath(__file__))
_extracted = os.path.dirname(_here)
if _extracted not in sys.path:
    sys.path.insert(1, _extracted)

# Now import uvicorn & app AFTER path is fixed
import uvicorn  # noqa: E402
from control_panel.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.getenv("CONTROL_PANEL_PORT", "5000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
