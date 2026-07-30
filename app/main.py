import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import FUNDS_SOURCE_FILE, USE_TURSO
from app.database import QuotaRecord, SessionLocal, check_database, init_db, is_turso_stream_error, reset_engine
from app.routes import router
from app.scheduler import start_scheduler, stop_scheduler
from app.service import run_scrape_and_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    db = SessionLocal()
    try:
        try:
            count = db.query(QuotaRecord).count()
        except Exception as exc:
            if USE_TURSO and is_turso_stream_error(exc):
                reset_engine()
                db.close()
                db = SessionLocal()
                count = db.query(QuotaRecord).count()
            else:
                raise
        if count == 0:
            logger.info("数据库为空，执行首次抓取...")
            await run_scrape_and_notify(FUNDS_SOURCE_FILE)
    finally:
        db.close()
    yield
    stop_scheduler()


app = FastAPI(title="基金额度监测", lifespan=lifespan)
app.include_router(router)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
static_dir = BASE_DIR / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
def health():
    try:
        check_database()
    except Exception as exc:
        if USE_TURSO and is_turso_stream_error(exc):
            reset_engine()
            try:
                check_database()
            except Exception as retry_exc:
                return JSONResponse(
                    status_code=503,
                    content={"status": "error", "detail": str(retry_exc)},
                )
        else:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": str(exc)},
            )
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
