"""Entity base shared by every platform (sensor, button).

Kept out of ``sensor.py`` so a platform never has to import another platform to reach it.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, CONF_COMPANY, DOMAIN
from .coordinator import MyEnergyCoordinator


def device_info(coordinator: MyEnergyCoordinator) -> DeviceInfo:
    """Describe the NMI's device; every entity of the entry attaches to it."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.nmi)},
        name=f"myEnergy {coordinator.site_name}",
        manufacturer=coordinator.config_entry.data.get(CONF_COMPANY) or "CitiPower / Powercor",
        model="Smart meter",
        serial_number=coordinator.nmi,
        suggested_area=None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://myenergy.powercor.com.au/s/energy-usage",
    )


class MyEnergyEntity(CoordinatorEntity[MyEnergyCoordinator]):
    """Common device information."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyEnergyCoordinator, key: str) -> None:
        """Attach to the NMI's device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{coordinator.nmi}_{key}"
        self._attr_device_info = device_info(coordinator)
        self._address = entry.data.get(CONF_ADDRESS)
