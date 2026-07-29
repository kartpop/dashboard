import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before importing any app module that reads env at import time
# (e.g. app.router.config) or at call time (the classifier's ANTHROPIC_API_KEY).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from app.auth.router import router as auth_router  # noqa: E402
from app.news import scheduler as news_scheduler  # noqa: E402
from app.router import scheduler as router_scheduler  # noqa: E402
from app.routers import calendar, news, scratch, tasks  # noqa: E402
from app.settings.router import router as settings_router  # noqa: E402

_log = logging.getLogger(__name__)

# In dev a fixed fallback keeps sessions stable across reloads; production MUST set
# SESSION_SECRET (an unset secret in prod would sign cookies with a public default).
_SESSION_SECRET = os.environ.get("SESSION_SECRET") or "dev-insecure-session-secret"
# Secure cookies require HTTPS — off for local http dev, on in prod (set COOKIE_SECURE=1).
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") not in ("0", "false", "")
_FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema is managed by Alembic (alembic upgrade head runs in docker-entrypoint.sh
    # and is documented as a manual step for local dev — don't call create_all here).
    router_scheduler.start()
    news_scheduler.start()
    try:
        yield
    finally:
        await news_scheduler.stop()
        await router_scheduler.stop()


app = FastAPI(title="Dashboard API", lifespan=lifespan)

# Signed, HttpOnly session cookie (goal 8). SameSite=Lax + JSON-body mutations is the
# accepted CSRF stance at this scale (goal-8 brief).
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    same_site="lax",
    https_only=_COOKIE_SECURE,
    session_cookie="dashboard_session",
)

# Cross-origin only matters in local dev (frontend :5173 → backend :8010); in prod the
# build is same-origin. Credentials must be allowed for the session cookie to flow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_FRONTEND_ORIGIN, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "PATCH", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException):
    detail = exc.detail
    if not (isinstance(detail, dict) and "code" in detail and "message" in detail):
        detail = {"code": "http_error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(tasks.router)
app.include_router(calendar.router)
app.include_router(scratch.router)
app.include_router(news.router)


# ── Serve the built frontend (goal 8: single container, one origin) ───────────
# When the Vite build exists (in the Docker image, or after `npm run build`), FastAPI
# serves it. `html=True` returns index.html for `/`. In local dev the dir is absent
# and Vite serves the SPA on :5173 — the API just runs headless.
_FRONTEND_DIST = Path(
    os.environ.get(
        "FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")
    )
)
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")
else:
    _log.info("frontend build not found at %s — API-only mode", _FRONTEND_DIST)
