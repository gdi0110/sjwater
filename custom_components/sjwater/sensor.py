from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN

import hashlib

def _account_id(username: str) -> str:
    """Return a short stable non-PII identifier derived from the username."""
    return hashlib.sha256(username.encode()).hexdigest()[:8]


def _freshness_attributes(coordinator) -> dict:
    """Return poll/data freshness attributes shared by all sensors.

    The utility publishes readings 6-24h late, so the time we last polled the
    portal (``last_polled_at``) routinely differs from the timestamp of the
    newest reading the portal actually had (``latest_data_point_at``). On a
    failed poll the coordinator retains the previous data, so
    ``last_polled_at`` reflects the last *successful* poll.
    """
    if coordinator.data is None:
        return {}
    return {
        "last_polled_at": coordinator.data.get("polled_at"),
        "latest_data_point_at": coordinator.data.get("timestamp"),
    }

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([
        SJWaterHubSensor(coordinator),
        SJWaterHubDailySensor(coordinator)
    ])

class SJWaterHubSensor(CoordinatorEntity, SensorEntity):
    """Representation of the Water Meter."""

    _attr_has_entity_name = True
    _attr_name = "Water Meter Total"
    _attr_device_class = SensorDeviceClass.WATER
    # TOTAL_INCREASING tells HA this is a monotonically increasing meter.
    # Any decrease is treated as a counter reset, never as negative usage.
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water"

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        
        acct = _account_id(coordinator.client.username)
        self.entity_id = f"sensor.sjwater_{acct}_water_usage"
        self._attr_unique_id = f"sjwater_{acct}_water_usage"

    @property
    def device_info(self):
        """Return device information to link entities up."""
        return {
            "identifiers": {(DOMAIN, _account_id(self.coordinator.client.username))},
            "name": "SJ Water Hub",
            "manufacturer": "SJ Water Company",
            "model": "Scraped Hub",
        }

    @property
    def native_value(self):
        """Return the continuously accumulating total meter reading."""
        if self.coordinator.data is not None:
            # We use the running sum calculated during statistic compilation
            # which ensures Home Assistant natively never sees a drop in value
            # avoiding artificial negative statistics in the Energy dashboard!
            return max(0.0, self.coordinator.data.get("current_sum", 0.0))
        return None

    @property
    def extra_state_attributes(self):
        """Include poll/data freshness timestamps in the state machine."""
        return _freshness_attributes(self.coordinator)


class SJWaterHubDailySensor(CoordinatorEntity, SensorEntity):
    """Representation of Today's Water Usage."""

    _attr_has_entity_name = True
    _attr_name = "Today's Water Usage"
    _attr_device_class = SensorDeviceClass.WATER
    # TOTAL (not TOTAL_INCREASING) paired with last_reset tells HA this value
    # resets daily.  TOTAL_INCREASING would treat the midnight reset (and any
    # mid-day decrease) as a counter roll-over, adding phantom consumption.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        acct = _account_id(coordinator.client.username)
        self.entity_id = f"sensor.sjwater_{acct}_todays_water_usage"
        self._attr_unique_id = f"sjwater_{acct}_todays_water_usage"
        self._last_reset_midnight: datetime | None = None

    @property
    def device_info(self):
        """Return device information to link entities up."""
        return {
            "identifiers": {(DOMAIN, _account_id(self.coordinator.client.username))},
            "name": "SJ Water Hub",
            "manufacturer": "SJ Water Company",
            "model": "Scraped Hub",
        }

    @property
    def native_value(self):
        """Return just the water used today."""
        if self.coordinator.data is not None:
            return max(0.0, self.coordinator.data.get("today_sum", 0.0))
        return None

    @property
    def extra_state_attributes(self):
        """Include poll/data freshness timestamps in the state machine."""
        return _freshness_attributes(self.coordinator)

    @property
    def last_reset(self):
        """Return the start of today so HA knows this counter resets daily."""
        now = dt_util.now()
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self._last_reset_midnight is None or self._last_reset_midnight.date() != today_midnight.date():
            self._last_reset_midnight = today_midnight
        return self._last_reset_midnight
