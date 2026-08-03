"""EDC sdílení elektřiny — sensor platform (config-entry based).

Each configured EAN becomes its own Home Assistant device with 3 sensors:
výroba (export), úspěšně sdíleno, podíl sdílené elektřiny.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EdcCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, EdcCoordinator] = data["coordinators"]

    entities: list[SensorEntity] = []
    for ean, coordinator in coordinators.items():
        device = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            name=f"EDC {ean}",
            manufacturer="EDC (neoficiální)",
            model="Sdílení elektřiny",
        )
        entities.append(
            EdcEnergySensor(coordinator, ean, device, "measured", "Výroba (export)", "mdi:solar-power")
        )
        entities.append(
            EdcEnergySensor(
                coordinator,
                ean,
                device,
                "shared",
                "Úspěšně sdíleno",
                "mdi:transmission-tower-export",
            )
        )
        entities.append(EdcShareRatioSensor(coordinator, ean, device))

    async_add_entities(entities)


class _EdcBaseSensor(SensorEntity):
    should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: EdcCoordinator, device: DeviceInfo):
        self._coordinator = coordinator
        self._attr_device_info = device

    @property
    def available(self) -> bool:
        data = self._coordinator.data
        if not data:
            return False
        return not data.get("ean_not_found", False)

    async def async_added_to_hass(self) -> None:
        self._coordinator.async_add_listener(self.async_write_ha_state)


class EdcEnergySensor(_EdcBaseSensor):
    """Daily total (measured production, or successfully shared volume) in kWh."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self, coordinator: EdcCoordinator, ean: str, device: DeviceInfo, key: str, name: str, icon: str
    ):
        super().__init__(coordinator, device)
        self._ean = ean
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"edc_sdileni_{ean}_{key}"

    @property
    def native_value(self):
        data = self._coordinator.data
        if not data:
            return None
        return round(data["latest"].get(self._key, 0.0), 3)

    @property
    def extra_state_attributes(self):
        data = self._coordinator.data or {}
        attrs = {
            "ean": self._ean,
            "datum": data.get("latest_date"),
            "pocet_znamych_dni": len(data.get("days", {})),
            "historie_dni": data.get("days"),
        }
        # The 15-min curve goes on the production sensor only. Both series
        # (výroba + sdíleno) are inside it, so putting it on both entities
        # would just duplicate a couple of kilobytes for no benefit.
        if self._key == "measured":
            attrs["detail_15min"] = data.get("intervals")
        return attrs


class EdcShareRatioSensor(_EdcBaseSensor):
    """Share of production that was successfully shared, in %."""

    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:percent"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EdcCoordinator, ean: str, device: DeviceInfo):
        super().__init__(coordinator, device)
        self._ean = ean
        self._attr_name = "Podíl sdílené elektřiny"
        self._attr_unique_id = f"edc_sdileni_{ean}_share_ratio"

    @property
    def native_value(self):
        data = self._coordinator.data
        if not data:
            return None
        latest = data["latest"]
        measured = latest.get("measured", 0.0)
        shared = latest.get("shared", 0.0)
        if measured <= 0:
            return 0.0
        return round(shared / measured * 100, 1)

    @property
    def extra_state_attributes(self):
        data = self._coordinator.data or {}
        return {"ean": self._ean, "datum": data.get("latest_date")}
