import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.api.routes import auth, categories, documents, files, misc, organizations, retention, reviews, workflows
from app.core.config import get_settings
from app.core.context import correlation_id_var
from app.core.errors import register_exception_handlers

log = logging.getLogger("app")


def _scheduler_tick() -> None:
    from app.db import session as s
    from app.services.jobs import drain, run_scheduled
    db = s.SessionLocal()
    try:
        run_scheduled(db)
        drain(db)
    except Exception:
        log.exception("scheduler tick failed")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.AUTO_CREATE_TABLES:  # development convenience only; use Alembic in real deployments
        from app.db.base import Base
        from app.db.session import engine
        import app.models  # noqa: F401
        Base.metadata.create_all(engine)
    task = None
    if settings.SCHEDULER_ENABLED:
        async def loop():
            while True:
                await asyncio.sleep(settings.SCHEDULER_INTERVAL_SECONDS)
                await asyncio.to_thread(_scheduler_tick)
        task = asyncio.create_task(loop())
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan,
                  description="Secure REST API for document lifecycle, versioning, multi-stage approvals, "
                              "retention and audit. Send `X-Organization-ID` if you belong to several organizations.")

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):
        cid = (request.headers.get("X-Correlation-ID") or uuid.uuid4().hex)[:64]
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        return response

    register_exception_handlers(app)
    api = APIRouter(prefix="/api")
    for module in (auth, organizations, categories, documents, files, workflows, reviews, retention, misc):
        api.include_router(module.router)
    app.include_router(api)

    @app.get("/health", tags=["Health"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
