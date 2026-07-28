from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfVolume

from custom_components.sjwater.sensor import (
    SJWaterHubSensor,
    SJWaterHubDailySensor,
    _account_id,
    async_setup_entry,
)
from custom_components.sjwater.const import DOMAIN


@pytest.fixture
def mock_coordinator():
    coord = MagicMock()
    coord.client.username = "test@example.com"
    coord.data = None
    return coord


@pytest.fixture
def total_sensor(mock_coordinator):
    return SJWaterHubSensor(mock_coordinator)


@pytest.fixture
def daily_sensor(mock_coordinator):
    return SJWaterHubDailySensor(mock_coordinator)


class TestAccountId:
    def test_derives_hash_from_username(self):
        result = _account_id("test@example.com")
        assert result == "973dfe46"
        assert len(result) == 8

    def test_consistent_for_same_username(self):
        assert _account_id("test@example.com") == _account_id("test@example.com")

    def test_different_for_different_usernames(self):
        assert _account_id("user1@example.com") != _account_id("user2@example.com")


class TestSJWaterHubSensor:
    async def test_returns_current_sum(self, total_sensor, mock_coordinator):
        mock_coordinator.data = {"current_sum": 150.5, "today_sum": 5.5, "timestamp": "2024-06-06T12:00:00"}
        assert total_sensor.native_value == 150.5

    async def test_returns_none_when_no_data(self, total_sensor, mock_coordinator):
        mock_coordinator.data = None
        assert total_sensor.native_value is None

    async def test_prevents_negative_values(self, total_sensor, mock_coordinator):
        mock_coordinator.data = {"current_sum": -10.0, "today_sum": 0.0}
        assert total_sensor.native_value == 0.0

    async def test_has_correct_device_class(self, total_sensor):
        assert total_sensor.device_class == SensorDeviceClass.WATER

    async def test_has_correct_state_class(self, total_sensor):
        assert total_sensor.state_class == SensorStateClass.TOTAL_INCREASING

    async def test_has_correct_unit(self, total_sensor):
        assert total_sensor.native_unit_of_measurement == UnitOfVolume.GALLONS

    async def test_has_correct_icon(self, total_sensor):
        assert total_sensor.icon == "mdi:water"

    async def test_has_entity_name(self, total_sensor):
        assert total_sensor.has_entity_name is True

    async def test_correct_entity_id_format(self, total_sensor, mock_coordinator):
        expected_id = "sensor.sjwater_973dfe46_water_usage"
        assert total_sensor.entity_id == expected_id
        assert total_sensor.unique_id == "sjwater_973dfe46_water_usage"

    async def test_device_info_structure(self, total_sensor):
        info = total_sensor.device_info
        assert info["identifiers"] == {(DOMAIN, "973dfe46")}
        assert info["name"] == "SJ Water Hub"
        assert info["manufacturer"] == "SJ Water Company"
        assert info["model"] == "Scraped Hub"

    async def test_extra_state_attributes_with_data(self, total_sensor, mock_coordinator):
        mock_coordinator.data = {
            "current_sum": 100.0,
            "timestamp": "2024-06-06T12:00:00",
            "polled_at": "2024-06-06T18:00:00",
        }
        attrs = total_sensor.extra_state_attributes
        assert attrs.get("latest_data_point_at") == "2024-06-06T12:00:00"
        assert attrs.get("last_polled_at") == "2024-06-06T18:00:00"
        assert "recorded_at" not in attrs

    async def test_extra_state_attributes_empty_when_no_data(self, total_sensor, mock_coordinator):
        mock_coordinator.data = None
        assert total_sensor.extra_state_attributes == {}

    async def test_extra_state_attributes_none_latest_when_no_readings(self, total_sensor, mock_coordinator):
        mock_coordinator.data = {
            "current_sum": 100.0,
            "timestamp": None,
            "polled_at": "2024-06-06T18:00:00",
        }
        attrs = total_sensor.extra_state_attributes
        assert attrs.get("latest_data_point_at") is None
        assert attrs.get("last_polled_at") == "2024-06-06T18:00:00"


class TestSJWaterHubDailySensor:
    async def test_returns_today_sum(self, daily_sensor, mock_coordinator):
        mock_coordinator.data = {"current_sum": 150.5, "today_sum": 5.5}
        assert daily_sensor.native_value == 5.5

    async def test_returns_none_when_no_data(self, daily_sensor, mock_coordinator):
        mock_coordinator.data = None
        assert daily_sensor.native_value is None

    async def test_prevents_negative_values(self, daily_sensor, mock_coordinator):
        mock_coordinator.data = {"current_sum": 100.0, "today_sum": -3.0}
        assert daily_sensor.native_value == 0.0

    async def test_has_correct_device_class(self, daily_sensor):
        assert daily_sensor.device_class == SensorDeviceClass.WATER

    async def test_has_correct_state_class(self, daily_sensor):
        assert daily_sensor.state_class == SensorStateClass.TOTAL

    async def test_has_correct_unit(self, daily_sensor):
        assert daily_sensor.native_unit_of_measurement == UnitOfVolume.GALLONS

    async def test_has_correct_icon(self, daily_sensor):
        assert daily_sensor.icon == "mdi:water-pump"

    async def test_correct_entity_id_format(self, daily_sensor, mock_coordinator):
        expected_id = "sensor.sjwater_973dfe46_todays_water_usage"
        assert daily_sensor.entity_id == expected_id
        assert daily_sensor.unique_id == "sjwater_973dfe46_todays_water_usage"

    async def test_device_info_matches_total_sensor(self, daily_sensor, total_sensor):
        assert daily_sensor.device_info == total_sensor.device_info

    async def test_extra_state_attributes_with_data(self, daily_sensor, mock_coordinator):
        mock_coordinator.data = {
            "current_sum": 150.5,
            "today_sum": 5.5,
            "timestamp": "2024-06-06T12:00:00",
            "polled_at": "2024-06-06T18:00:00",
        }
        attrs = daily_sensor.extra_state_attributes
        assert attrs.get("latest_data_point_at") == "2024-06-06T12:00:00"
        assert attrs.get("last_polled_at") == "2024-06-06T18:00:00"

    async def test_extra_state_attributes_empty_when_no_data(self, daily_sensor, mock_coordinator):
        mock_coordinator.data = None
        assert daily_sensor.extra_state_attributes == {}

    async def test_extra_state_attributes_match_total_sensor(self, daily_sensor, total_sensor, mock_coordinator):
        mock_coordinator.data = {
            "current_sum": 150.5,
            "today_sum": 5.5,
            "timestamp": "2024-06-06T12:00:00",
            "polled_at": "2024-06-06T18:00:00",
        }
        assert daily_sensor.extra_state_attributes == total_sensor.extra_state_attributes

    async def test_last_reset_returns_midnight(self, daily_sensor):
        with patch("custom_components.sjwater.sensor.dt_util.now") as mock_now:
            mock_now.return_value = datetime(2024, 6, 6, 14, 30, 0)
            result = daily_sensor.last_reset
            assert result.hour == 0
            assert result.minute == 0
            assert result.second == 0

    async def test_last_reset_caches_same_day(self, daily_sensor):
        with patch("custom_components.sjwater.sensor.dt_util.now") as mock_now:
            mock_now.return_value = datetime(2024, 6, 6, 14, 30, 0)
            result1 = daily_sensor.last_reset
            result2 = daily_sensor.last_reset
            assert result1 is result2

    async def test_last_reset_invalidates_on_day_change(self, daily_sensor):
        with patch("custom_components.sjwater.sensor.dt_util.now") as mock_now:
            mock_now.return_value = datetime(2024, 6, 6, 14, 30, 0)
            day1 = daily_sensor.last_reset

            mock_now.return_value = datetime(2024, 6, 7, 14, 30, 0)
            day2 = daily_sensor.last_reset

            assert day1 != day2
            assert day2.day == 7


class TestSJWaterHubSensorEdgeCases:
    async def test_today_sum_key_missing_defaults_zero(self, mock_coordinator):
        mock_coordinator.data = {"current_sum": 100.0}
        sensor = SJWaterHubDailySensor(mock_coordinator)
        assert sensor.native_value == 0.0


class TestAsyncSetupEntry:
    async def test_creates_both_sensors(self):
        mock_hass = MagicMock()
        mock_entry = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.client.username = "test@example.com"
        mock_entry.runtime_data = mock_coordinator
        mock_add_entities = MagicMock()

        await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 2
        assert isinstance(entities[0], SJWaterHubSensor)
        assert isinstance(entities[1], SJWaterHubDailySensor)
