#!/usr/bin/env python3
"""discover.py — run once to find switch MAC and confirm port numbers.

Usage: nix develop --command python3 discover.py
"""

import asyncio
import getpass

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration


async def main() -> None:
    host = input("Controller host [unifi]: ") or "unifi"
    port = int(input("Controller port [8443]: ") or "8443")
    site = input("Site id [default]: ") or "default"
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    async with aiohttp.ClientSession() as session:
        config = Configuration(
            session,
            host,
            username=username,
            password=password,
            port=port,
            site=site,
            ssl_context=False,  # self-signed / internal CA controller cert
        )
        ctrl = Controller(config)
        await ctrl.login()
        await ctrl.devices.update()

        for mac, device in ctrl.devices.items():
            print(f"\nMAC: {mac}")
            print(f"  id:    {device.id}")
            print(f"  Model: {device.model}")
            print(f"  Name:  {device.name}")
            for port in device.port_table:
                print(
                    f"  Port {port.get('port_idx'):>2}: "
                    f"{port.get('name', ''):<20} poe_mode={port.get('poe_mode', 'n/a')}"
                )


asyncio.run(main())
