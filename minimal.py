#!/usr/bin/env python3
"""
AP Schedule Controller — MVP
Turns PoE off at 23:59 CT, on at 06:00 CT.
No web UI. Run with: nix develop --command python3 minimal.py
"""

import asyncio
import logging
import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from aiounifi.models.device import DeviceSetPoePortModeRequest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(
    os.environ.get("AP_CONTROLLER_CONFIG", Path(__file__).parent / "config.toml")
)


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


async def set_poe(mode: str, cfg: dict) -> None:
    """Set all configured ports to mode ('off' | 'auto')."""
    async with aiohttp.ClientSession() as session:
        config = Configuration(
            session,
            cfg["controller"]["host"],
            username=cfg["controller"]["username"],
            password=cfg["controller"]["password"],
            port=cfg["controller"]["port"],
            site=cfg["controller"]["site"],
            ssl_context=False,
        )
        ctrl = Controller(config)
        await ctrl.login()
        await ctrl.devices.update()

        for port_cfg in cfg["ports"]:
            mac = port_cfg["device_mac"]
            idx = port_cfg["port_idx"]
            device = ctrl.devices.get(mac)
            if device is None:
                log.error(f"Device {mac} not found")
                continue
            request = DeviceSetPoePortModeRequest.create(
                device, port_idx=idx, mode=mode
            )
            await ctrl.request(request)
            log.info(f"Set port {idx} on {mac} to poe={mode}")


def job_poe_off():
    cfg = load_config()
    log.info("Scheduled: turning APs off")
    asyncio.run(set_poe("off", cfg))


def job_poe_on():
    cfg = load_config()
    log.info("Scheduled: turning APs on")
    asyncio.run(set_poe("auto", cfg))


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])

    jobstores = {
        "default": SQLAlchemyJobStore(url="sqlite:///ap_controller.db")
    }
    scheduler = BlockingScheduler(jobstores=jobstores, timezone=tz)

    if not scheduler.get_job("poe_off"):
        scheduler.add_job(
            job_poe_off,
            CronTrigger(
                hour=cfg["schedule"]["off_hour"],
                minute=cfg["schedule"]["off_minute"],
                timezone=tz,
            ),
            id="poe_off",
            replace_existing=True,
        )

    if not scheduler.get_job("poe_on"):
        scheduler.add_job(
            job_poe_on,
            CronTrigger(
                hour=cfg["schedule"]["on_hour"],
                minute=cfg["schedule"]["on_minute"],
                timezone=tz,
            ),
            id="poe_on",
            replace_existing=True,
        )

    log.info(
        f"Scheduler started. "
        f"off={cfg['schedule']['off_hour']}:{cfg['schedule']['off_minute']:02d} CT  "
        f"on={cfg['schedule']['on_hour']}:{cfg['schedule']['on_minute']:02d} CT"
    )
    scheduler.start()


if __name__ == "__main__":
    main()
