"""FastAPI app: JSON status/snooze API today, a mobile-first HTML page (the
"/" route + templates/) is the next piece to add on top of this.

No authentication — LAN-only home network is the trust boundary (see
README's security model).
"""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from .config import load_config, load_credentials
from .scheduler import PoeScheduler, build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    username, password = load_credentials()
    app.state.scheduler = await build_scheduler(cfg, username, password)
    try:
        yield
    finally:
        await app.state.scheduler.close()


app = FastAPI(lifespan=lifespan)


def get_scheduler(request: Request) -> PoeScheduler:
    return request.app.state.scheduler


class SnoozeRequest(BaseModel):
    minutes: Literal[30, 60, 120]


@app.get("/status")
async def status(scheduler: PoeScheduler = Depends(get_scheduler)) -> dict:
    return scheduler.status()


@app.post("/snooze")
async def snooze(
    body: SnoozeRequest, scheduler: PoeScheduler = Depends(get_scheduler)
) -> dict:
    await scheduler.snooze(body.minutes)
    return scheduler.status()


@app.post("/on")
async def turn_on_now(scheduler: PoeScheduler = Depends(get_scheduler)) -> dict:
    await scheduler.turn_on_now()
    return scheduler.status()


@app.post("/off")
async def turn_off_now(scheduler: PoeScheduler = Depends(get_scheduler)) -> dict:
    await scheduler.turn_off_now()
    return scheduler.status()


@app.post("/ports/{mac}/{idx}/snooze")
async def snooze_port(
    mac: str,
    idx: int,
    body: SnoozeRequest,
    scheduler: PoeScheduler = Depends(get_scheduler),
) -> dict:
    try:
        await scheduler.snooze_port(mac, idx, body.minutes)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return scheduler.status()


@app.post("/ports/{mac}/{idx}/on")
async def turn_on_port_now(
    mac: str, idx: int, scheduler: PoeScheduler = Depends(get_scheduler)
) -> dict:
    try:
        await scheduler.turn_on_port_now(mac, idx)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return scheduler.status()


@app.post("/ports/{mac}/{idx}/off")
async def turn_off_port_now(
    mac: str, idx: int, scheduler: PoeScheduler = Depends(get_scheduler)
) -> dict:
    try:
        await scheduler.turn_off_port_now(mac, idx)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return scheduler.status()
