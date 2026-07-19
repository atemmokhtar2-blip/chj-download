from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os
from ..auth import require_owner
from ..db_utils import get_dashboard_stats, get_recent_activity, get_downloads_chart
from ..config import CONTROL_PANEL_DIR

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: dict = Depends(require_owner)):
    stats = get_dashboard_stats()
    activity = get_recent_activity(12)
    chart = get_downloads_chart(7)
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats, "activity": activity,
        "chart": chart, "active_page": "dashboard"
    })


@router.get("/api/stats")
async def api_stats(session: dict = Depends(require_owner)):
    return get_dashboard_stats()


@router.get("/api/activity")
async def api_activity(session: dict = Depends(require_owner)):
    return get_recent_activity(20)


@router.get("/api/chart")
async def api_chart(days: int = 7, session: dict = Depends(require_owner)):
    return get_downloads_chart(days)
