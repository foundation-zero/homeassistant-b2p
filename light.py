import logging

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    PLATFORM_SCHEMA_BASE,
    ColorMode,
    LightEntity,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import restore_state
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_B2P_CHANNEL,
    CONF_B2P_HOST,
    CONF_B2P_PDC,
    B2P_RESTORE_SAVE_DELAY,
    DATA_RESTORE_SAVER,
    DOMAIN,
)
from .hub import B2pPdc, async_get_hub

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA_BASE.extend(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_B2P_HOST): str,
        vol.Required(CONF_B2P_PDC): int,
        vol.Required(CONF_B2P_CHANNEL): int,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    hub = async_get_hub(hass)
    await hub.async_start()
    pdc, created = hub.get_or_create_pdc(config[CONF_B2P_HOST], config[CONF_B2P_PDC])
    if created:
        pdc.probe()
        hass.async_create_task(
            async_load_platform(
                hass,
                Platform.BINARY_SENSOR,
                DOMAIN,
                {CONF_B2P_HOST: pdc.host, CONF_B2P_PDC: pdc.number},
                {},
            )
        )

    async_add_entities(
        [
            B2pLight(
                config[CONF_NAME],
                config[CONF_B2P_CHANNEL],
                pdc,
            )
        ]
    )


class B2pLight(RestoreEntity, LightEntity):
    """Representation of a B2P light."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, name: str, channel: int, pdc: B2pPdc):
        self._is_on = False
        self._attr_name = name
        self._attr_unique_id = f"{pdc.id}_{channel}"
        self._pdc = pdc
        self._channel = channel

    @property
    def is_on(self):
        """If the switch is currently on or off."""
        return self.brightness > 0

    @property
    def brightness(self):
        return self._pdc.brightness(self._channel)

    async def async_added_to_hass(self) -> None:
        """Restore the last known brightness to the PDC.

        After a power outage the lights may have defaulted to full on (emergency
        supply), so push the last known state, on or off, back to the PDC.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state not in (STATE_ON, STATE_OFF):
            return
        if last_state.state == STATE_ON:
            brightness = last_state.attributes.get(ATTR_BRIGHTNESS) or 255
        else:
            brightness = 0
        await self._set_brightness(brightness)

    async def _set_brightness(self, brightness: int) -> None:
        await self._pdc.set_brightness(self._channel, brightness)
        self.async_write_ha_state()
        _restore_saver(self.hass).async_schedule_call()

    async def async_turn_on(self, **kwargs):
        await self._set_brightness(kwargs.get(ATTR_BRIGHTNESS, 255))

    async def async_turn_off(self, **kwargs):
        await self._set_brightness(0)


def _restore_saver(hass: HomeAssistant) -> Debouncer:
    """Return a debouncer that persists entity states shortly after a change.

    Home Assistant only dumps restore states every 15 minutes and on a clean
    shutdown, neither of which is guaranteed to capture changes before a blackout.
    """
    if (saver := hass.data.get(DATA_RESTORE_SAVER)) is None:

        async def _save() -> None:
            await restore_state.async_get(hass).async_dump_states()

        saver = hass.data[DATA_RESTORE_SAVER] = Debouncer(
            hass,
            _LOGGER,
            cooldown=B2P_RESTORE_SAVE_DELAY,
            immediate=False,
            function=_save,
        )
    return saver
