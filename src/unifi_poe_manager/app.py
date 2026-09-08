"""FastAPI app: a mobile-first HTML page (htmx-driven, no page reloads on
snooze/turn-on/turn-off) plus the same actions as a plain JSON API for any
other consumer. Every mutating/status route returns JSON normally, or a
rendered HTML fragment when called by htmx (detected via the `HX-Request`
header) — see _status_response().

No authentication — LAN-only home network is the trust boundary (see
README's security model).
"""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from .config import load_config, load_credentials
from .scheduler import PoeScheduler, build_scheduler

PACKAGE_DIR = Path(__file__).parent

TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _hhmm(value: str | None) -> str:
    """ISO datetime string -> "HH:MM" for display; used by templates."""
    if not value:
        return "—"
    return datetime.fromisoformat(value).strftime("%H:%M")


TEMPLATES.env.filters["hhmm"] = _hhmm


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
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")


def get_scheduler(request: Request) -> PoeScheduler:
    return request.app.state.scheduler


class SnoozeRequest(BaseModel):
    minutes: Literal[30, 60, 120]


def _status_response(request: Request, scheduler: PoeScheduler) -> Response | dict:
    """The JSON status dict, or (for an htmx-driven request) the rendered
    status fragment htmx will swap into the page in place."""
    data = scheduler.status()
    if request.headers.get("hx-request") == "true":
        return TEMPLATES.TemplateResponse(request, "_status.html", {"status": data})
    return data


@app.get("/", response_class=Response)
async def index(request: Request, scheduler: PoeScheduler = Depends(get_scheduler)):
    return TEMPLATES.TemplateResponse(
        request, "index.html", {"status": scheduler.status()}
    )


@app.get("/status")
async def status(request: Request, scheduler: PoeScheduler = Depends(get_scheduler)):
    return _status_response(request, scheduler)


@app.post("/snooze")
async def snooze(
    request: Request, body: SnoozeRequest, scheduler: PoeScheduler = Depends(get_scheduler)
):
    await scheduler.snooze(body.minutes)
    return _status_response(request, scheduler)


@app.post("/on")
async def turn_on_now(request: Request, scheduler: PoeScheduler = Depends(get_scheduler)):
    await scheduler.turn_on_now()
    return _status_response(request, scheduler)


@app.post("/off")
async def turn_off_now(request: Request, scheduler: PoeScheduler = Depends(get_scheduler)):
    await scheduler.turn_off_now()
    return _status_response(request, scheduler)


@app.post("/ports/{mac}/{idx}/snooze")
async def snooze_port(
    request: Request,
    mac: str,
    idx: int,
    body: SnoozeRequest,
    scheduler: PoeScheduler = Depends(get_scheduler),
):
    try:
        await scheduler.snooze_port(mac, idx, body.minutes)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return _status_response(request, scheduler)


@app.post("/ports/{mac}/{idx}/on")
async def turn_on_port_now(
    request: Request, mac: str, idx: int, scheduler: PoeScheduler = Depends(get_scheduler)
):
    try:
        await scheduler.turn_on_port_now(mac, idx)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return _status_response(request, scheduler)


@app.post("/ports/{mac}/{idx}/off")
async def turn_off_port_now(
    request: Request, mac: str, idx: int, scheduler: PoeScheduler = Depends(get_scheduler)
):
    try:
        await scheduler.turn_off_port_now(mac, idx)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No configured port {idx} on {mac}")
    return _status_response(request, scheduler)
