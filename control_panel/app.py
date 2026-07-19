import os
import asyncio
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import CONTROL_PANEL_DIR, SECRET_KEY, OWNER_ID, PUBLIC_URL
from .auth import (create_session, get_session, require_owner,
                   SESSION_COOKIE, SESSION_MAX_AGE, NotAuthenticated, ACCESS_TOKEN)
from .routers import (dashboard, users, broadcast, db_manager,
                      files, logs_router, system, updates, github_router, search)

app = FastAPI(title="TitanX Control Panel", docs_url=None, redoc_url=None)

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_MAX_AGE)

_static = os.path.join(CONTROL_PANEL_DIR, "static")
_templates_dir = os.path.join(CONTROL_PANEL_DIR, "templates")
app.mount("/static", StaticFiles(directory=_static), name="static")
templates = Jinja2Templates(directory=_templates_dir)

app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(broadcast.router)
app.include_router(db_manager.router)
app.include_router(files.router)
app.include_router(logs_router.router)
app.include_router(system.router)
app.include_router(updates.router)
app.include_router(github_router.router)
app.include_router(search.router)


def _public_base() -> str:
    return PUBLIC_URL or ""


def _access_url() -> str:
    base = _public_base()
    return f"{base}/panel?k={ACCESS_TOKEN}" if base else f"/panel?k={ACCESS_TOKEN}"


# ── Exception handlers ──────────────────────────────────────────────────────

@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/panel", status_code=302)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return templates.TemplateResponse(request, "access.html", {
        "owner_id": OWNER_ID,
        "access_url": _access_url(),
        "error": str(getattr(exc, "detail", "وصول مرفوض")),
    }, status_code=403)


# ── Access route (token-based) ──────────────────────────────────────────────

@app.get("/panel", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def panel_access(request: Request, k: str = ""):
    # Already authenticated → go to dashboard
    session = get_session(request)
    if session and session.get("uid") == OWNER_ID:
        return RedirectResponse("/", status_code=302)

    # Token provided → validate
    if k:
        if k == ACCESS_TOKEN:
            token = create_session(OWNER_ID)
            response = RedirectResponse("/", status_code=302)
            response.set_cookie(SESSION_COOKIE, token,
                                max_age=SESSION_MAX_AGE, httponly=True,
                                secure=True, samesite="lax")
            return response
        # Wrong token
        return templates.TemplateResponse(request, "access.html", {
            "owner_id": OWNER_ID,
            "access_url": _access_url(),
            "error": "رمز الدخول غير صحيح",
        })

    # No token → show access page
    return templates.TemplateResponse(request, "access.html", {
        "owner_id": OWNER_ID,
        "access_url": _access_url(),
        "error": None,
    })


# ── Legacy login redirect ───────────────────────────────────────────────────

@app.get("/login")
async def login_redirect():
    return RedirectResponse("/panel", status_code=302)


@app.get("/auth/callback")
async def auth_callback_redirect():
    return RedirectResponse("/panel", status_code=302)


# ── Logout ──────────────────────────────────────────────────────────────────

@app.get("/logout")
async def logout():
    response = RedirectResponse("/panel", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "panel": "TitanX Control Panel"}
