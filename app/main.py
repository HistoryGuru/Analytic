import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers.debater import router as debater_router

logger = logging.getLogger("ld_scout")
settings = get_settings()

app = FastAPI(
    title="LD Scout API",
    description="On-demand Lincoln-Douglas debate scouting: live-queries Opencaselist for "
    "disclosed arguments and Tabroom for round results, then uses a Groq-hosted LLM to "
    "draft scouting notes and strategy.",
    version="0.1.0",
)


@app.middleware("http")
async def catch_unhandled_exceptions(request: Request, call_next):
    """
    Catches anything that escapes a route handler and returns a normal
    JSON 500 instead of letting it propagate raw.

    This has to be registered as an `@app.middleware("http")` BEFORE
    `app.add_middleware(CORSMiddleware, ...)` below -- not as an
    `@app.exception_handler(Exception)`. Verified empirically: Starlette
    wires a bare-`Exception` handler to its outermost error layer
    regardless of registration order, which sits OUTSIDE CORSMiddleware, so
    the response comes back with no Access-Control-Allow-Origin header --
    and the browser reports a "CORS error" that has nothing to do with your
    CORS_ORIGINS setting. Registering it this way, before CORSMiddleware is
    added, puts it INSIDE the CORS layer instead, so caught exceptions
    still get the header like any other response.
    """
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {exc}"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(debater_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "message": "LD Scout API is running. See /docs for the interactive API explorer.",
        "example": "/api/debater?q=Jane%20Doe&school=Harvard-Westlake",
    }
