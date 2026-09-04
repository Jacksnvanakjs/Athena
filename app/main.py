import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
    FUNDS_SOURCE_FILE,
    SELF_HEAL_ENABLED,
    SELF_HEAL_STARTUP_DELAY_SEC,
    TURSO_CONNECT_TIMEOUT_SEC,
    TURSO_RECONNECT_INTERVAL_SEC,
    USE_TURSO,
)
from app.database import (
    QuotaRecord,
    SessionLocal,
    get_active_turso_url,
    get_db_backend,
    is_db_ready,
    is_turso_stream_error,
    reset_engine,
    try_startup_db,
)
from app.routes import router
from app.scheduler import start_scheduler, stop_scheduler, scheduler_status
from app.service import run_scrape_and_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


async def _maybe_first_scrape() -> None:
    """库空时做首次抓取；DB 未就绪则跳过。

    Turso 下启动阶段不做：父进程 SessionLocal 握手会占 GIL，把整站卡死。
    """
    if USE_TURSO:
        return
    if not is_db_ready():
        return
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
    except Exception as exc:
        logger.warning("启动首次抓取跳过: %s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 绝不在 lifespan 里同步等 Turso：libsql 握手会占 GIL，
    # 超时后探测线程也返回不了，会把整站卡在 "Waiting for application startup"。
    start_scheduler()

    async def _boot_and_reconnect_turso() -> None:
        # 持续探测：Turso 多节点优先；全挂则 SQLite 兜底；之后仍尝试升回 Turso
        attempt = 0
        while True:
            attempt += 1
            if attempt == 1:
                logger.info("后台探测数据库（timeout=%.0fs）...", float(TURSO_CONNECT_TIMEOUT_SEC))
            else:
                interval = max(5.0, float(TURSO_RECONNECT_INTERVAL_SEC))
                backend = get_db_backend()
                logger.info(
                    "数据库后台重连 #%s（当前=%s，间隔 %.0fs）...",
                    attempt - 1,
                    backend,
                    interval,
                )
                await asyncio.sleep(interval)
            try:
                ok = await asyncio.to_thread(try_startup_db, TURSO_CONNECT_TIMEOUT_SEC)
            except Exception as exc:
                logger.warning("数据库探测异常: %s", exc)
                ok = False
            if ok and get_db_backend() == "turso":
                logger.info("Turso 已就绪（%s）", get_active_turso_url() or "primary")
                await _maybe_first_scrape()
                # 不 return：继续低频探测，防止节点挂掉后无法切兜底
                await asyncio.sleep(max(60.0, float(TURSO_RECONNECT_INTERVAL_SEC) * 3))
            elif ok and get_db_backend() == "sqlite":
                logger.info("未配置 Turso，使用本地 SQLite")
                return

    asyncio.create_task(_boot_and_reconnect_turso())

    if SELF_HEAL_ENABLED:
        async def _startup_self_heal() -> None:
            # DB 未就绪时多等一会，避免刚启动就打满超时
            delay = max(5, SELF_HEAL_STARTUP_DELAY_SEC)
            if not is_db_ready():
                delay = max(delay, int(TURSO_RECONNECT_INTERVAL_SEC) * 2)
            await asyncio.sleep(delay)
            if not is_db_ready():
                logger.warning("启动后数据自检跳过：数据库尚未就绪")
                return
            try:
                from app.self_heal import run_self_heal

                logger.info("启动后数据自检开始（delay=%ss）...", delay)
                result = await run_self_heal()
                logger.info("启动后数据自检完成: %s", result.get("audit_after") or result)
            except Exception as exc:
                logger.warning("启动后数据自检失败: %s", exc)

        asyncio.create_task(_startup_self_heal())

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


@app.middleware("http")
async def turso_circuit_breaker(request: Request, call_next):
    """DB 未就绪时：HTML 照常返回，API 快速 503，避免父进程去握手 Turso 卡死整站。"""
    path = request.url.path
    # 允许手动重连/状态探测（否则 degraded 时无法触发重连）
    if path in ("/api/db/reconnect", "/api/db/status", "/health"):
        return await call_next(request)
    if (
        USE_TURSO
        and not is_db_ready()
        and (path.startswith("/api") or path.startswith("/scrape"))
    ):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "database not ready; reconnecting",
                "status": "degraded",
                "turso": True,
            },
        )
    return await call_next(request)


@app.get("/health")
def health():
    """
    DB 未就绪时仍返回 200 + degraded，避免编排器因 503 反复杀进程；
    调度器异常仍用 503。
    Turso 就绪后不做同步 SELECT：libsql 握手会占 GIL，拖死整站响应。
    """
    db_ok = is_db_ready()
    backend = get_db_backend()
    detail = None if db_ok else "turso not ready; reconnecting"

    sched = scheduler_status()
    if not db_ok:
        body = {
            "status": "degraded",
            "db": False,
            "db_backend": backend,
            "detail": detail,
            "scheduler": sched,
            "turso": USE_TURSO,
        }
        return body

    body = {
        "status": "ok",
        "db": True,
        "db_backend": backend,
        "turso_url": get_active_turso_url() or None,
        "scheduler": sched,
        "turso": USE_TURSO,
    }
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
