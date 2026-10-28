"""Shared UDP endpoint and per-PDC health tracking for the b2p integration."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import re
from time import monotonic

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    B2P_FADE_TIME,
    B2P_OFFLINE_AFTER,
    B2P_PDC_THROTTLE,
    B2P_POLL_INTERVAL,
    B2P_PORT,
    B2P_UNSOLICITED_WINDOW,
    DATA_HUB,
)

_LOGGER = logging.getLogger(__name__)

_FADE_RESPONSE = re.compile(r"@FADE_RESPONSE\s*\(\s*(\d+)\s*\)")
_ID = re.compile(r"@ID\s*\(\s*(\d+)\s*\)\s*DESCRIPTION\((.*)\)")


def pdc_id(host: str, pdc: int) -> str:
    """Return the identifier of a PDC."""
    return f"{host}_{pdc}"


class B2pPdc:
    """Representation of a B2P PDC.

    Keeps track of the channels to prevent overloading PDC with too many commands,
    and of whether the PDC is reachable.
    """

    def __init__(self, hub: "B2pHub", host: str, pdc: int) -> None:
        self._hub = hub
        self._host = host
        self._pdc = pdc
        self._brightnesses: dict[int, int] = {}
        self._last_message = 0.0
        self._is_scheduled = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._created = monotonic()
        self.last_seen_monotonic: float | None = None
        self.last_seen: datetime | None = None
        self.description: str | None = None
        # None until we've either heard from the PDC or given up waiting for it
        self.online: bool | None = None

    @property
    def id(self) -> str:
        return pdc_id(self._host, self._pdc)

    @property
    def host(self) -> str:
        return self._host

    @property
    def number(self) -> int:
        return self._pdc

    def brightness(self, channel: int) -> int:
        return self._brightnesses.get(channel, 0)

    async def set_brightness(self, channel: int, brightness: int) -> None:
        """Set the brightness of a channel."""
        self._brightnesses[channel] = brightness
        await self._send_when_due()

    async def _send_when_due(self) -> None:
        if self._is_scheduled:  # rely on the scheduled update to send brightnesses
            return
        self._is_scheduled = True
        try:
            # negative sleeps execute immediately
            await asyncio.sleep(
                B2P_PDC_THROTTLE - (monotonic() - self._last_message)
            )
            self._send_state()
        finally:
            self._is_scheduled = False

    def _send_state(self) -> None:
        channels = [
            f"{channel}={int(brightness / 2.55)}"
            for channel, brightness in self._brightnesses.items()
        ]
        self._hub.send(
            f"@FADE( {self._pdc}, {int(B2P_FADE_TIME * 1000)} ) SOLL {{ {", ".join(channels)} }}\n",
            self._host,
        )
        self._last_message = monotonic()

    def probe(self) -> None:
        """Ask the PDC to identify itself."""
        self._hub.send("@WHO_IS_THERE\n", self._host)

    async def async_resend(self) -> None:
        """Push the last known state to the PDC, e.g. after it restarted."""
        if self._brightnesses:
            _LOGGER.info("Restoring last known state of PDC %s", self.id)
            await self._send_when_due()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for changes in the PDC's health."""
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _set_online(self, online: bool) -> None:
        changed = self.online != online
        self.online = online
        if changed:
            for update in list(self._listeners):
                update()

    @callback
    def async_seen(self, description: str | None = None, *, restarted: bool = False) -> None:
        """Record that we heard from the PDC."""
        was_offline = self.online is False
        self.last_seen_monotonic = monotonic()
        self.last_seen = dt_util.utcnow()
        if description is not None:
            self.description = description
        self._set_online(True)
        if was_offline or restarted:
            self._hub.hass.async_create_task(self.async_resend())

    @callback
    def async_check_health(self) -> None:
        """Mark the PDC offline when it hasn't been heard from for a while."""
        since = self.last_seen_monotonic or self._created
        if monotonic() - since > B2P_OFFLINE_AFTER:
            if self.online is not False:
                _LOGGER.warning("PDC %s is not responding", self.id)
            self._set_online(False)
        self.probe()

    @callback
    def async_handle_fade_response(self) -> None:
        # A response we didn't ask for is the PDC's power-up announcement
        unsolicited = monotonic() - self._last_message > B2P_UNSOLICITED_WINDOW
        self.async_seen(restarted=unsolicited)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: "B2pHub") -> None:
        self._hub = hub

    def datagram_received(self, data: bytes, addr) -> None:
        self._hub.handle_datagram(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("B2P UDP error: %s", exc)


class B2pHub:
    """One UDP endpoint shared by all PDCs.

    Sending and receiving on the same port lets us see replies and the
    broadcast PDCs send on power-up.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.pdcs: dict[str, B2pPdc] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._unsub_poll: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        async with self._lock:
            if self._transport is not None:
                return
            loop = asyncio.get_running_loop()
            try:
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self),
                    local_addr=("0.0.0.0", B2P_PORT),
                    allow_broadcast=True,
                    reuse_port=True,
                )
            except OSError as err:
                _LOGGER.warning(
                    "Could not listen on UDP port %s (%s); PDC power-up "
                    "announcements will not be seen",
                    B2P_PORT,
                    err,
                )
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self),
                    local_addr=("0.0.0.0", 0),
                    allow_broadcast=True,
                )
            self._unsub_poll = async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=B2P_POLL_INTERVAL)
            )
            self.hass.bus.async_listen_once("homeassistant_stop", self._async_stop)

    @callback
    def _async_stop(self, _event=None) -> None:
        if self._unsub_poll:
            self._unsub_poll()
        if self._transport:
            self._transport.close()
            self._transport = None

    def send(self, message: str, host: str) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(message.encode("ascii"), (host, B2P_PORT))
        except OSError as err:
            # e.g. network not up yet right after a power outage
            _LOGGER.warning("Could not send to %s: %s", host, err)

    def get_or_create_pdc(self, host: str, pdc: int) -> tuple[B2pPdc, bool]:
        key = pdc_id(host, pdc)
        if (existing := self.pdcs.get(key)) is not None:
            return existing, False
        self.pdcs[key] = new = B2pPdc(self, host, pdc)
        return new, True

    @callback
    def _async_poll(self, _now=None) -> None:
        for pdc in self.pdcs.values():
            pdc.async_check_health()

    @callback
    def handle_datagram(self, data: bytes, source: str) -> None:
        text = data.decode("ascii", errors="ignore").strip("\x00 \r\n")
        if match := _FADE_RESPONSE.match(text):
            if pdc := self._find(source, int(match[1])):
                pdc.async_handle_fade_response()
        elif match := _ID.match(text):
            if pdc := self._find(source, int(match[1])):
                pdc.async_seen(description=match[2])

    def _find(self, source: str, number: int) -> B2pPdc | None:
        if (pdc := self.pdcs.get(pdc_id(source, number))) is not None:
            return pdc
        # configured host may be a hostname or broadcast address
        matches = [p for p in self.pdcs.values() if p.number == number]
        return matches[0] if len(matches) == 1 else None


@callback
def async_get_hub(hass: HomeAssistant) -> B2pHub:
    """Return the shared hub."""
    if (hub := hass.data.get(DATA_HUB)) is None:
        hub = hass.data[DATA_HUB] = B2pHub(hass)
    return hub
