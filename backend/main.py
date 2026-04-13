import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from core.scheduler import start_scheduler, stop_scheduler
from db.database import init_db
from services.manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("alldebrid-client")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AllDebrid-Client...")
    await init_db()
    try:
        await manager.import_existing_magnets()
    except Exception as e:
        logger.warning(f"Initial AllDebrid import skipped: {e}")
    await start_scheduler()
    yield
    logger.info("Shutting down AllDebrid-Client...")
    await stop_scheduler()


app = FastAPI(
    title="AllDebrid-Client",
    description="Automated torrent downloading via AllDebrid",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

# ── Static files resolution ───────────────────────────────────────────────────
# Candidates in priority order. A valid static dir MUST contain index.html.
# Path("") / Path(".") are intentionally excluded — they match anything and
# caused the bug where "Serving static files from: ." + 404 on all routes.
_here = Path(__file__).parent

_candidates = []

# 1. Explicit env override (only if non-empty)
_env = os.getenv("STATIC_DIR", "").strip()
if _env:
    _candidates.append(Path(_env))

# 2. Relative to this file: <repo>/backend/../frontend/static
_candidates.append(_here.parent / "frontend" / "static")

# 3. Docker layout: /app/frontend/static  (Dockerfile copies frontend/ → /app/frontend/)
_candidates.append(Path("/app/frontend/static"))

# 4. Flat Docker layout: /app/static
_candidates.append(Path("/app/static"))

def _is_valid(p: Path) -> bool:
    return p.is_dir() and (p / "index.html").exists()

_static = next((p for p in _candidates if _is_valid(p)), None)

if _static is None:
    tried = ", ".join(str(p) for p in _candidates)
    raise RuntimeError(
        f"Frontend index.html not found. Tried: [{tried}]. "
        "Fix your Docker build (COPY frontend/ /app/frontend/) "
        "or set the STATIC_DIR environment variable."
    )

logger.info(f"Serving static files from: {_static}")
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
