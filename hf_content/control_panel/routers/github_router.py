import os
import subprocess
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import httpx
from ..auth import require_owner
from ..config import CONTROL_PANEL_DIR, PROJECT_ROOT

router = APIRouter(prefix="/github")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))


def _get_config() -> dict:
    return {
        "token": os.getenv("GITHUB_TOKEN", ""),
        "repo": os.getenv("GITHUB_REPO", ""),
    }


def _git_run(*args, cwd: str = PROJECT_ROOT) -> dict:
    try:
        r = subprocess.run(
            ["git"] + list(args), cwd=cwd,
            capture_output=True, text=True, timeout=30)
        return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


def _get_git_info() -> dict:
    info = {}
    r = _git_run("log", "--oneline", "-10")
    info["commits"] = r["stdout"].split("\n") if r["ok"] and r["stdout"] else []
    r = _git_run("status", "--short")
    info["status"] = r["stdout"] if r["ok"] else ""
    r = _git_run("branch", "--show-current")
    info["branch"] = r["stdout"] if r["ok"] else "unknown"
    r = _git_run("remote", "-v")
    info["remotes"] = r["stdout"] if r["ok"] else ""
    return info


@router.get("", response_class=HTMLResponse)
async def github_page(request: Request, session: dict = Depends(require_owner)):
    cfg = _get_config()
    git_info = _get_git_info()
    return templates.TemplateResponse(request, "github.html", {
        "cfg": cfg, "git_info": git_info, "active_page": "github"
    })


@router.get("/api/info")
async def api_info(session: dict = Depends(require_owner)):
    return _get_git_info()


@router.post("/api/pull")
async def api_pull(session: dict = Depends(require_owner)):
    r = _git_run("pull", "--rebase")
    return {"ok": r["ok"], "output": r["stdout"] or r["stderr"]}


@router.post("/api/push")
async def api_push(request: Request, session: dict = Depends(require_owner)):
    body = await request.json()
    msg = body.get("message", "Update via TitanX Control Panel")
    r1 = _git_run("add", "-A")
    r2 = _git_run("commit", "-m", msg)
    r3 = _git_run("push")
    output = "\n".join(filter(None, [r1["stdout"], r2["stdout"], r3["stdout"],
                                      r1["stderr"], r2["stderr"], r3["stderr"]]))
    return {"ok": r3["ok"], "output": output}


@router.post("/api/commit")
async def api_commit(request: Request, session: dict = Depends(require_owner)):
    body = await request.json()
    msg = body.get("message", "Commit via TitanX")
    paths = body.get("paths", ["."])
    r1 = _git_run("add", *paths)
    r2 = _git_run("commit", "-m", msg)
    return {"ok": r2["ok"], "output": r2["stdout"] or r2["stderr"]}


@router.get("/api/diff")
async def api_diff(session: dict = Depends(require_owner)):
    r = _git_run("diff", "--stat")
    return {"ok": r["ok"], "diff": r["stdout"]}


@router.post("/api/configure")
async def api_configure(request: Request, session: dict = Depends(require_owner)):
    body = await request.json()
    repo = body.get("repo", "").strip()
    token = body.get("token", "").strip()
    name = body.get("name", "TitanX Bot").strip()
    email = body.get("email", "bot@titanx.local").strip()
    results = []
    if name:
        r = _git_run("config", "user.name", name)
        results.append(f"name: {'ok' if r['ok'] else r['stderr']}")
    if email:
        r = _git_run("config", "user.email", email)
        results.append(f"email: {'ok' if r['ok'] else r['stderr']}")
    if repo and token:
        url = f"https://{token}@github.com/{repo}.git"
        r = _git_run("remote", "set-url", "origin", url)
        if not r["ok"]:
            r = _git_run("remote", "add", "origin", url)
        results.append(f"remote: {'ok' if r['ok'] else r['stderr']}")
    return {"ok": True, "results": results}


@router.get("/api/gh/repos")
async def api_gh_repos(session: dict = Depends(require_owner)):
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse({"error": "لا يوجد GitHub Token"}, status_code=400)
    async with httpx.AsyncClient() as client:
        r = await client.get("https://api.github.com/user/repos?per_page=30&sort=updated",
                              headers={"Authorization": f"token {token}"})
        if r.status_code == 200:
            repos = [{"name": x["full_name"], "private": x["private"]} for x in r.json()]
            return {"repos": repos}
        return JSONResponse({"error": "فشل الاتصال بـ GitHub"}, status_code=400)
