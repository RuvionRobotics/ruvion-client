from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import AsyncIterator, Optional, Union

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_ruvion._udp.local."
DEFAULT_PORT = 4433
EXPECTED_ALPN = "ruvion-motion/1"

_RESOLVE_TIMEOUT_MS = 3000
_ADDR_RETRY_TIMEOUT_MS = 2000
_UNSET = "unset"

IPAddress = Union[IPv4Address, IPv6Address]


@dataclass
class Controller:
    serial: str
    addresses: list[IPAddress]
    port: int
    state: str  # "added" | "updated" | "removed"
    model: str = ""
    mode: str = ""
    hardware: str = ""
    firmware: str = ""
    protocol_version: int = 0
    alpn: str = ""
    cert_expires: Optional[datetime] = None
    name: Optional[str] = None
    location: Optional[str] = None
    hostname: Optional[str] = None
    raw_txt: dict[str, str] = field(default_factory=dict)

    @property
    def alpn_compatible(self) -> bool:
        return self.alpn == EXPECTED_ALPN


def _decode_txt(properties: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (properties or {}).items():
        key = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
        if v is None:
            out[key] = ""
        elif isinstance(v, bytes):
            out[key] = v.decode("utf-8", errors="replace")
        else:
            out[key] = str(v)
    return out


def _unset_or(v: Optional[str]) -> Optional[str]:
    if v is None or v == "" or v == _UNSET:
        return None
    return v


def _parse_int(v: Optional[str]) -> int:
    if not v:
        return 0
    try:
        return int(v)
    except ValueError:
        return 0


def _parse_cert_expires(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        log.debug("cert_expires has unexpected format: %r", v)
        return None


def _extract_addresses(info: AsyncServiceInfo) -> list[IPAddress]:
    raw: list[str] = []
    try:
        raw = list(info.parsed_scoped_addresses())
    except Exception as e:  # noqa: BLE001
        log.debug("parsed_scoped_addresses failed: %s", e)

    if not raw:
        for addr in info.addresses or []:
            if len(addr) == 4:
                raw.append(socket.inet_ntop(socket.AF_INET, addr))
            elif len(addr) == 16:
                raw.append(socket.inet_ntop(socket.AF_INET6, addr))

    out: list[IPAddress] = []
    for s in raw:
        try:
            out.append(ip_address(s))
        except ValueError:
            log.debug("could not parse address: %s", s)
    return out


def _info_to_controller(info: AsyncServiceInfo, state: str) -> Controller:
    serial = info.name.removesuffix("." + SERVICE_TYPE).removesuffix(".")
    addresses = _extract_addresses(info)
    txt = _decode_txt(info.properties or {})

    return Controller(
        serial=serial,
        addresses=addresses,
        port=info.port or DEFAULT_PORT,
        state=state,
        model=txt.get("model", ""),
        mode=txt.get("type", ""),
        hardware=txt.get("hardware", ""),
        firmware=txt.get("firmware", ""),
        protocol_version=_parse_int(txt.get("protocol")),
        alpn=txt.get("alpn", ""),
        cert_expires=_parse_cert_expires(txt.get("cert_expires")),
        name=_unset_or(txt.get("name")),
        location=_unset_or(txt.get("loc")),
        hostname=info.server,
        raw_txt=txt,
    )


async def discover(
    *,
    robot_type: Optional[str] = None,
    ip_version: IPVersion = IPVersion.All,
) -> AsyncIterator[Controller]:
    """Browse the local network for Ruvion controllers.

    Yields a Controller each time a controller is announced, updated, or removed.
    The state field reflects which event triggered the yield ("added", "updated",
    "removed"). Runs until the caller breaks out of the iteration.

    robot_type filters by mDNS subtype (e.g. "single" or "humanoid") by browsing
    _<robot_type>._sub._ruvion._udp.local. instead of the base service type.
    """
    browse_type = (
        f"_{robot_type}._sub.{SERVICE_TYPE}" if robot_type else SERVICE_TYPE
    )

    queue: asyncio.Queue[Controller] = asyncio.Queue()
    aiozc = AsyncZeroconf(ip_version=ip_version)

    async def _resolve(name: str, state: str) -> None:
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        ok = await info.async_request(aiozc.zeroconf, _RESOLVE_TIMEOUT_MS)
        if not ok:
            log.debug("resolve failed for %s", name)
            return
        if not _extract_addresses(info):
            log.debug("no addresses yet for %s, retrying", name)
            await info.async_request(aiozc.zeroconf, _ADDR_RETRY_TIMEOUT_MS)
        await queue.put(_info_to_controller(info, state))

    def _on_change(zeroconf, service_type, name, state_change):  # noqa: ARG001
        if state_change is ServiceStateChange.Removed:
            serial = name.removesuffix("." + SERVICE_TYPE).removesuffix(".")
            queue.put_nowait(
                Controller(serial=serial, addresses=[], port=0, state="removed")
            )
            return
        state = "added" if state_change is ServiceStateChange.Added else "updated"
        asyncio.ensure_future(_resolve(name, state))

    browser = AsyncServiceBrowser(
        aiozc.zeroconf,
        browse_type,
        handlers=[_on_change],
    )

    try:
        while True:
            yield await queue.get()
    finally:
        await browser.async_cancel()
        await aiozc.async_close()


async def discover_once(
    *,
    timeout: float = 5.0,
    robot_type: Optional[str] = None,
    ip_version: IPVersion = IPVersion.All,
) -> list[Controller]:
    """One-shot scan: collect controllers for `timeout` seconds and return the
    latest state per serial. Removed controllers are dropped from the result.
    """
    latest: dict[str, Controller] = {}

    async def _collect() -> None:
        async for controller in discover(
            robot_type=robot_type, ip_version=ip_version
        ):
            if controller.state == "removed":
                latest.pop(controller.serial, None)
            else:
                latest[controller.serial] = controller

    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return list(latest.values())
