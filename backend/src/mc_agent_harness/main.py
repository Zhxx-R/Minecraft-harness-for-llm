from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from mc_agent_harness.api.routes.configuration import router as configuration_router
from mc_agent_harness.api.routes.dashboard import router as dashboard_router
from mc_agent_harness.api.routes.health import router as health_router
from mc_agent_harness.api.routes.launcher import router as launcher_router


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(title="Minecraft Agent Harness", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(SQLAlchemyError, database_exception_handler)
    app.include_router(health_router, prefix="/api")
    app.include_router(dashboard_router, prefix="/api")
    app.include_router(launcher_router, prefix="/api")
    app.include_router(configuration_router, prefix="/api")
    return app


async def database_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Return a clear service-unavailable response for database connectivity failures."""

    _ = request
    return JSONResponse(
        status_code=503,
        content={
            "detail": "database_unavailable",
            "message": str(exc).splitlines()[0],
        },
    )


app = create_app()
