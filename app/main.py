import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import settings
from app.core.container import Container
from app.core.errors import AppError, ConfigError
from app.core.metrics import render_metrics
from app.core.middleware import RequestMiddleware
from app.routers import api_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("app")

API_ROLES = ("llm", "condense_llm", "embedder", "reranker", "vectors")  # the API never needs the VLM / OCR


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = Container(settings, redis=Redis.from_url(settings.redis_url, decode_responses=True))
    container.providers.validate(API_ROLES)  # a bad provider/model/key in .env fails here, at startup
    app.state.container = container
    log.info("providers: %s", container.providers.describe())
    try:
        await container.providers.vectors.ensure_collection()
    except Exception:
        log.exception("vector store not reachable at startup; /health/ready will report it")

    async def warm():
        try:
            await container.providers.embedder.warmup()
            await container.providers.reranker.warmup()
        except Exception:
            log.exception("model warmup failed")

    task = asyncio.create_task(warm())  # load local models without delaying startup
    yield
    task.cancel()
    await container.providers.aclose()
    await container.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="School Textbooks RAG", version="2.1.0", lifespan=lifespan)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    app.add_middleware(RequestMiddleware)  # added last = outermost: it also measures errors raised by other layers

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint():
        # Reachable only inside the docker network: nginx answers 404 for /metrics from the internet.
        body, content_type = render_metrics()
        return Response(body, media_type=content_type)

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(ConfigError)
    async def config_error(_: Request, exc: ConfigError):
        log.error("configuration error: %s", exc)
        return JSONResponse(status_code=500, content={"error": {"code": "misconfigured", "message": "Server misconfigured"}})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": {"code": "internal", "message": "Internal server error"}})

    app.include_router(api_router)
    return app


app = create_app()
