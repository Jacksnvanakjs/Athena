import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import FUNDS_SOURCE_FILE, USE_TURSO
from app.database import QuotaRecord, SessionLocal, check_database, init_db, is_turso_stream_error, reset_engine
from app.routes import router
from app.scheduler import start_scheduler, stop_scheduler, scheduler_status
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

    if os.environ.get("ENV", "").lower() != "development":
        async def _warm_heatmap_sina() -> None:
            try:
                from app.heatmap import warm_sina_spot_cache

                ok = await warm_sina_spot_cache()
                if ok:
                    logger.info("热力图新浪现货缓存预热完成")
            except Exception as exc:
                logger.warning("热力图新浪缓存预热跳过: %s", exc)

        asyncio.create_task(_warm_heatmap_sina())
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
    sched = scheduler_status()
    body = {"status": "ok", "scheduler": sched}
    if sched["enabled"] and not sched["running"]:
        return JSONResponse(status_code=503, content={**body, "detail": "scheduler not running"})
    return body


@app.get("/")
async def index():
    return RedirectResponse(url="/deals", status_code=302)


@app.get("/funds", response_class=HTMLResponse)
async def funds_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page(request: Request):
    return templates.TemplateResponse("heatmap.html", {"request": request})


@app.get("/deals", response_class=HTMLResponse)
async def deals_page(request: Request):
    return templates.TemplateResponse("deals.html", {"request": request})
