from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
import os, secrets

from app.database import init_db
from app.api import servers, destinations, jobs, runs
from app.api import auth_routes, settings as settings_api, stats as stats_api

_key_file = ".secret_key"
if os.path.exists(_key_file):
    with open(_key_file) as f:
        _secret = f.read().strip()
else:
    _secret = os.environ.get("BACKUP_SECRET_KEY") or secrets.token_hex(32)
    with open(_key_file, "w") as f:
        f.write(_secret)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_admin_exists()
    from app.scheduler import start
    start()
    yield
    from app.scheduler import stop
    stop()


def _ensure_admin_exists():
    """Se non esiste nessun utente, crea l'admin di default al primo avvio."""
    from app.database import SessionLocal
    from app.models import User
    from app.auth import hash_password
    db = SessionLocal()
    try:
        if not db.query(User).first():
            admin = User(
                username="admin",
                email="",
                hashed_password=hash_password("changeme"),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            print("[INFO] Utente admin creato (password: changeme). Cambiala subito!")
    finally:
        db.close()


app = FastAPI(title="Backup-All CRI Catania", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_secret, https_only=False, max_age=86400)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── API routers ───────────────────────────────────────
app.include_router(servers.router)
app.include_router(destinations.router)
app.include_router(jobs.router)
app.include_router(runs.router)
app.include_router(auth_routes.router)
app.include_router(settings_api.router)
app.include_router(stats_api.router)


# ── Auth middleware per pagine UI ─────────────────────
def _check_session(request: Request):
    """Restituisce user_id dalla sessione o None."""
    return request.session.get("user_id")


# ── UI pages ──────────────────────────────────────────

@app.get("/")
async def home(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")


@app.get("/login")
async def login_page(request: Request):
    if _check_session(request):
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/setup")
async def setup_page(request: Request):
    """Wizard primo avvio."""
    return templates.TemplateResponse("setup_wizard.html", {"request": request})


@app.get("/dashboard")
async def dashboard(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/wizard/server")
async def wizard_server(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("wizard_server.html", {"request": request})


@app.get("/wizard/destination")
async def wizard_destination(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("wizard_destination.html", {"request": request})


@app.get("/wizard/job")
async def wizard_job(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("wizard_job.html", {"request": request})


@app.get("/jobs")
async def jobs_page(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("jobs.html", {"request": request})


@app.get("/history")
async def history_page(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("history.html", {"request": request})


@app.get("/logs/{run_id}")
async def logs_page(run_id: int, request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("logs.html", {"request": request, "run_id": run_id})


@app.get("/settings")
async def settings_page(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/reports")
async def reports_page(request: Request):
    if not _check_session(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("reports.html", {"request": request})
