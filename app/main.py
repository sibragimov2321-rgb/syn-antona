from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().assert_safe_runtime()
    yield


app = FastAPI(title="Сын Антона", version="0.1.0", lifespan=lifespan)
app.include_router(router)
