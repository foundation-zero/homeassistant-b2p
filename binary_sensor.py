"""Connectivity sensor for B2P PDCs."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_B2P_HOST, CONF_B2P_PDC
from .hub import B2pPdc, async_get_hub, pdc_id


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the PDC sensor; created automatically by the light platform."""
    if discovery_info is None:
        return
    pdc = async_get_hub(hass).pdcs[
        pdc_id(discovery_info[CONF_B2P_HOST], discovery_info[CONF_B2P_PDC])
    ]
    async_add_entities([B2pPdcOnline(pdc)])


class B2pPdcOnline(BinarySensorEntity):
    """Whether a PDC is reachable."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, pdc: B2pPdc) -> None:
        self._pdc = pdc
        self._attr_name = f"B2P PDC {pdc.number} ({pdc.host})"
        self._attr_unique_id = f"{pdc.id}_online"

    @property
    def is_on(self) -> bool | None:
        return self._pdc.online

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_seen": self._pdc.last_seen,
            "description": self._pdc.description,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._pdc.async_add_listener(self.async_write_ha_state)
        )
