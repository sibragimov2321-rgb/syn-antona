from fastapi import APIRouter
from pydantic import BaseModel

from app.domain.models import BotMode

router = APIRouter()


class BotStatus(BaseModel):
    mode: BotMode = BotMode.DEMO
    state: str = "PAUSED"
    live_trading_available: bool = False


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "trading": "paper-only"}


@router.get("/v1/bot/status", response_model=BotStatus)
def bot_status() -> BotStatus:
    return BotStatus()
