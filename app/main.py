from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers.debater import router as debater_router

settings = get_settings()

app = FastAPI(
    title="LD Scout API",
    description="On-demand Lincoln-Douglas debate scouting: live-queries Tournaments.Tech "
    "and Opencaselist for a debater's record and disclosed arguments, then uses a "
    "Groq-hosted LLM to draft scouting notes and strategy.",
    version="0.1.0",
)

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
