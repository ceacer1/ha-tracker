"""Sensors exposing the location data of Home Assistant persons."""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import DOMAIN

_SENSOR_FIELDS = (
    ("location", "Location", None, None),
    ("latitude", "Latitude", None, "°"),
    ("longitude", "Longitude", None, "°"),
    ("gps_accuracy", "GPS accuracy", SensorDeviceClass.DISTANCE, "m"),
    ("altitude", "Altitude", SensorDeviceClass.DISTANCE, "m"),
    ("speed", "Speed", SensorDeviceClass.SPEED, "m/s"),
    ("course", "Course", None, "°"),
    ("vertical_accuracy", "Vertical accuracy", SensorDeviceClass.DISTANCE, "m"),
    ("battery_level", "Battery", SensorDeviceClass.BATTERY, "%"),
    ("gps_timestamp", "GPS timestamp", None, None),
    ("source_type", "Tracker source", None, None),
    ("address", "Address", None, None),
)
_NUMERIC_FIELDS = {
    "latitude",
    "longitude",
    "gps_accuracy",
    "altitude",
    "speed",
    "course",
    "vertical_accuracy",
    "battery_level",
}
_LOGGER = logging.getLogger(__name__)


def _person_trackers(attributes: dict[str, Any]) -> list[str]:
    """Return a person's associated tracker ids, with their current source first."""
    source = attributes.get("source")
    trackers = attributes.get("device_trackers", [])
    if isinstance(trackers, str):
        trackers = [trackers]
    if not isinstance(trackers, (list, tuple)):
        trackers = []

    candidates = []
    if isinstance(source, str) and source.startswith("device_tracker."):
        candidates.append(source)
    candidates.extend(
        tracker
        for tracker in trackers
        if isinstance(tracker, str) and tracker.startswith("device_tracker.")
    )
    return list(dict.fromkeys(candidates))


def _as_number(value: Any) -> float | None:
    """Convert a finite numeric sensor value, if possible."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _address(attributes: dict[str, Any]) -> str | None:
    """Resolve common string or structured address attributes."""
    for key in ("address", "formatted_address", "display_name"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            display_name = value.get("display_name")
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
            parts = [str(part) for part in value.values() if part]
            if parts:
                return ", ".join(parts)
    return None


class HATrackerCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Periodically resolve each person's selected device tracker."""

    def __init__(self, hass: HomeAssistant, update_interval: int) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Read current person and device-tracker states from Home Assistant."""
        data = {}
        for person in self.hass.states.async_all("person"):
            person_attributes = dict(person.attributes)
            trackers = _person_trackers(person_attributes)
            tracker_id = next(
                (
                    entity_id
                    for entity_id in trackers
                    if self.hass.states.get(entity_id) is not None
                ),
                None,
            )
            tracker = self.hass.states.get(tracker_id) if tracker_id else None
            tracker_attributes = dict(tracker.attributes) if tracker else {}

            # Prefer the person entity's resolved location and fill in GPS details
            # from its active tracker when those values are not present there.
            attributes = dict(tracker_attributes)
            attributes.update(
                {
                    key: value
                    for key, value in person_attributes.items()
                    if value is not None
                }
            )
            if "gps_accuracy" not in attributes and "accuracy" in tracker_attributes:
                attributes["gps_accuracy"] = tracker_attributes["accuracy"]
            if "speed" not in attributes:
                if "speedMps" in tracker_attributes:
                    attributes["speed"] = tracker_attributes["speedMps"]
                elif "velocity" in tracker_attributes:
                    speed = _as_number(tracker_attributes["velocity"])
                    if speed is not None:
                        attributes["speed"] = speed / 3.6

            data[person.entity_id] = {
                "name": person_attributes.get("friendly_name", person.entity_id),
                "state": person.state,
                "tracker_entity": tracker_id,
                "device_trackers": trackers,
                "attributes": attributes,
            }
        return data


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up person sensors using the configured update interval."""
    config = {**entry.data, **entry.options}
    coordinator = HATrackerCoordinator(
        hass, int(config.get("update_interval", 10))
    )
    await coordinator.async_config_entry_first_refresh()

    added: set[str] = set()

    def add_missing_persons() -> None:
        """Add sensors when new persons with trackers appear."""
        entities = []
        for person_id, person_data in (coordinator.data or {}).items():
            if not person_data["tracker_entity"]:
                continue
            for field, label, device_class, unit in _SENSOR_FIELDS:
                unique_id = f"{entry.entry_id}_{person_id}_{field}"
                if unique_id in added:
                    continue
                added.add(unique_id)
                entities.append(
                    HATrackerSensor(
                        coordinator,
                        person_id,
                        field,
                        label,
                        device_class,
                        unit,
                        unique_id,
                    )
                )
        if entities:
            async_add_entities(entities)

    add_missing_persons()
    entry.async_on_unload(coordinator.async_add_listener(add_missing_persons))


class HATrackerSensor(CoordinatorEntity[HATrackerCoordinator], SensorEntity):
    """A single location, GPS, or address value for a person."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HATrackerCoordinator,
        person_id: str,
        field: str,
        label: str,
        device_class: SensorDeviceClass | None,
        unit: str | None,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._person_id = person_id
        self._field = field
        self._attr_name = label
        self._attr_unique_id = unique_id
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if field in _NUMERIC_FIELDS:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def device_info(self) -> DeviceInfo:
        """Group each person's sensors under one Home Assistant device."""
        person_data = (self.coordinator.data or {}).get(self._person_id, {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._person_id)},
            name=person_data.get("name", self._person_id),
            manufacturer="Home Assistant",
            model="Person location",
        )

    @property
    def available(self) -> bool:
        """Mark the sensor unavailable when its person or tracker is gone."""
        person_data = (self.coordinator.data or {}).get(self._person_id)
        return bool(person_data and person_data.get("tracker_entity"))

    @property
    def native_value(self) -> str | float | None:
        """Return the current value for this sensor."""
        person_data = (self.coordinator.data or {}).get(self._person_id)
        if not person_data:
            return None
        if self._field == "location":
            return person_data.get("state")

        attributes = person_data.get("attributes", {})
        if self._field == "address":
            return _address(attributes)
        if self._field == "gps_accuracy":
            return _as_number(attributes.get("gps_accuracy", attributes.get("accuracy")))
        if self._field == "speed":
            speed = attributes.get("speed", attributes.get("speedMps"))
            if speed is None and "velocity" in attributes:
                velocity = _as_number(attributes["velocity"])
                return velocity / 3.6 if velocity is not None else None
            return _as_number(speed)
        if self._field == "battery_level":
            value = next(
                (
                    attributes[key]
                    for key in (
                        "battery_level",
                        "battery_percentage",
                        "battery",
                        "batteryLevel",
                        "battery_state",
                    )
                    if attributes.get(key) is not None
                ),
                None,
            )
            if isinstance(value, str):
                value = value.strip().rstrip("%")
            battery_level = _as_number(value)
            if battery_level is not None and 0 < battery_level <= 1:
                battery_level *= 100
            return battery_level
        if self._field == "course":
            value = next(
                (
                    attributes[key]
                    for key in ("course", "direction", "bearing", "heading")
                    if attributes.get(key) is not None
                ),
                None,
            )
            return _as_number(value)
        if self._field == "gps_timestamp":
            value = next(
                (
                    attributes[key]
                    for key in ("gps_timestamp", "timestamp")
                    if attributes.get(key) is not None
                ),
                None,
            )
            return value if isinstance(value, str) else None
        if self._field == "source_type":
            value = attributes.get("source_type")
            return value if isinstance(value, str) else None
        return _as_number(attributes.get(self._field))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose all available location and GPS attributes on each sensor."""
        person_data = (self.coordinator.data or {}).get(self._person_id, {})
        attributes = dict(person_data.get("attributes", {}))
        attributes.update(
            {
                "person_entity_id": self._person_id,
                "tracker_entity_id": person_data.get("tracker_entity"),
                "device_trackers": person_data.get("device_trackers", []),
            }
        )
        return attributes
