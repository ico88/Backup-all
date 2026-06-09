from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import os

from app.database import init_db
from app.api import servers, destinations, jobs, runs

# Carica .secret_key se esiste (persistenza chiave tra riavvii)
_key_file = ".secret_key"
if os.path.exists(_key_file):
    with open(_key_file) as f:
        os.environ.setdefault("BACKUP_SECRET_KEY", f.read().strip())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.scheduler import start
    start()
    yield
    from app.scheduler import stop
    stop()


app = FastAPI(title="Backup-All CRI Catania", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── API routers ───────────────────────────────────────
app.include_router(servers.router)
app.include_router(destinations.router)
app.include_router(jobs.router)
app.include_router(runs.router)


# ── UI pages ──────────────────────────────────────────

@app.get("/")
async def home():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/wizard/server")
async def wizard_server(request: Request):
    return templates.TemplateResponse("wizard_server.html", {"request": request})


@app.get("/wizard/destination")
async def wizard_destination(request: Request):
    return templates.TemplateResponse("wizard_destination.html", {"request": request})


@app.get("/wizard/job")
async def wizard_job(request: Request):
    return templates.TemplateResponse("wizard_job.html", {"request": request})


@app.get("/jobs")
async def jobs_page(request: Request):
    return templates.TemplateResponse("jobs.html", {"request": request})


@app.get("/history")
async def history_page(request: Request):
    return templates.TemplateResponse("history.html", {"request": request})


@app.get("/logs/{run_id}")
async def logs_page(run_id: int, request: Request):
    return templates.TemplateResponse("logs.html", {"request": request, "run_id": run_id})
