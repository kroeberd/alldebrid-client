import asyncio
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("alldebrid-client")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AllDebrid-Client...")
    await init_db()
    await start_scheduler()
    yield
    logger.info("Shutting down AllDebrid-Client...")
    await stop_scheduler()


app = FastAPI(
    title="AllDebrid-Client",
    description="Automated torrent downloading via AllDebrid",
    version="1.0.0",
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

# Resolve frontend/static: env override > relative to this file > Docker fallback
_here = Path(__file__).parent
_candidates = [
    Path(os.getenv("STATIC_DIR", "")),
    _here.parent / "frontend" / "static",
    Path("/app/frontend/static"),
]
_static = next((p for p in _candidates if p.exists()), None)
if _static is None:
    raise RuntimeError("Frontend static directory not found. Set STATIC_DIR env var.")

logger.info(f"Serving static files from: {_static}")
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
