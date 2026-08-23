from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.sjwater.coordinator import SJWaterHubCoordinator

# LastUpdated marker far enough ahead that it never cuts off test history.
PUBLISHED_EVERYTHING = "2099-01-01T00:00:00+00:00"


def _api_data(history, now, last_updated=PUBLISHED_EVERYTHING):
    return {
        "gallons": float(history[-1]["state"]) if history else 0.0,
        "timestamp": now,
        "last_updated": last_updated,
        "history": history,
    }


class TestAsyncInitialize:
    async def test_restores_state_from_store(self, coordinator, mock_store):
        mock_store.async_load.return_value = {
            "current_sum": 100.0,
            "last_processed_start": 1717000000,
        }

        await coordinator.async_initialize()

        assert coordinator._current_sum == 100.0
        assert coordinator._last_processed_start == 1717000000
        assert coordinator._initialized is True

    async def test_handles_empty_store(self, coordinator, mock_store):
        mock_store.async_load.return_value = None

        await coordinator.async_initialize()

        assert coordinator._current_sum is None
        assert coordinator._last_processed_start is None
        assert coordinator._initialized is True

    async def test_handles_missing_keys_in_store(self, coordinator, mock_store):
        mock_store.async_load.return_value = {"some_other_key": "value"}

        await coordinator.async_initialize()

        assert coordinator._current_sum is None
        assert coordinator._last_processed_start is None


class TestAsyncUpdateData:
    async def test_processes_new_entries(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        # Older than FINALIZATION_LAG so the entries are finalized/persisted
        history = [
            {"start": now - timedelta(hours=50), "state": "1.5"},
            {"start": now - timedelta(hours=49), "state": "2.5"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.5,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 4.0
        assert "today_sum" in result
        assert "timestamp" in result
        mock_store.async_save.assert_called_once()
        mock_import.assert_called_once()

    async def test_skips_already_processed_entries(self, coordinator, mock_store):
        coordinator._current_sum = 10.0
        coordinator._last_processed_start = 1717000000

        now = dt_util.utcnow()
        history = [
            {"start": datetime.fromtimestamp(1716990000, tz=timezone.utc), "state": "1.0"},
            {"start": datetime.fromtimestamp(1717005000, tz=timezone.utc), "state": "2.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 12.0
        mock_store.async_save.assert_called_once()

    async def test_empty_history_returns_defaults(self, coordinator, mock_store):
        coordinator._current_sum = 0.0

        now = dt_util.utcnow()
        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": [],
        })):
            with patch.object(coordinator, "_import_stats") as mock_import:
                result = await coordinator._async_update_data()

        assert result["current_sum"] == 0.0
        assert result["today_sum"] == 0.0
        mock_store.async_save.assert_not_called()
        mock_import.assert_not_called()

    async def test_persists_state_after_update(self, coordinator, mock_store):
        coordinator._current_sum = 5.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        # Older than FINALIZATION_LAG so the entry is finalized/persisted
        history = [
            {"start": now - timedelta(hours=49), "state": "3.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 3.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()

        mock_store.async_save.assert_called_once_with({
            "current_sum": 8.0,
            "last_processed_start": int(history[0]["start"].timestamp()),
        })

    async def test_propagates_api_error(self, coordinator, mock_store):
        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(side_effect=Exception("API error")),
        ):
            with pytest.raises(Exception, match="API error"):
                await coordinator._async_update_data()

    async def test_current_sum_none_on_first_run(self, coordinator, mock_store):
        coordinator._current_sum = None
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=1), "state": "3.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 3.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 3.0

    async def test_non_datetime_start_skipped(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": "not-a-datetime", "state": "5.0"},
            {"start": now - timedelta(hours=1), "state": "2.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 2.0

    async def test_negative_state_clamped_in_today_sum(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=2), "state": "-5.0"},
            {"start": now - timedelta(hours=1), "state": "3.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 3.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 3.0
        assert result["today_sum"] == 3.0


class TestFinalizationWindow:
    async def test_future_placeholder_entries_ignored(self, coordinator, mock_store):
        coordinator._current_sum = 5.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now + timedelta(hours=2), "state": "0.0"},
            {"start": now + timedelta(hours=3), "state": "7.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 0.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 5.0
        mock_store.async_save.assert_not_called()
        mock_import.assert_not_called()

    async def test_provisional_entries_reported_but_not_persisted(self, coordinator, mock_store):
        coordinator._current_sum = 10.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=2), "state": "1.0"},
            {"start": now - timedelta(hours=1), "state": "2.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        # Provisional readings reach the sensor and the statistics table...
        assert result["current_sum"] == 13.0
        mock_import.assert_called_once()
        rows = mock_import.call_args[0][0]
        assert [r["sum"] for r in rows] == [11.0, 13.0]
        # ...but never advance the persisted watermark/sum, so late-arriving
        # revisions for these hours are re-processed on the next poll.
        mock_store.async_save.assert_not_called()

    async def test_finalized_and_provisional_split(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        old = now - timedelta(hours=50)
        history = [
            {"start": old, "state": "4.0"},
            {"start": now - timedelta(hours=1), "state": "2.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        assert result["current_sum"] == 6.0
        mock_store.async_save.assert_called_once_with({
            "current_sum": 4.0,
            "last_processed_start": int(old.timestamp()),
        })

    async def test_reported_sum_never_decreases(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None
        coordinator._last_reported_sum = 20.0

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=1), "state": "2.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.0,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats"):
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        # A downward provisional revision must not make the TOTAL_INCREASING
        # sensor dip (HA would read it as a meter reset).
        assert result["current_sum"] == 20.0


class TestImportCutoff:
    async def test_in_progress_hour_excluded(self, coordinator, mock_store):
        """The bucket for the current hour is never imported (issue #10)."""
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow().replace(minute=30, second=0, microsecond=0)
        this_hour = now.replace(minute=0)
        history = [
            {"start": this_hour - timedelta(hours=1), "state": "2.0"},
            {"start": this_hour, "state": "0.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value=_api_data(history, now))):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.utcnow", return_value=now):
                    with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                        result = await coordinator._async_update_data()

        assert result["current_sum"] == 2.0
        rows = mock_import.call_args[0][0]
        assert [r["start"] for r in rows] == [this_hour - timedelta(hours=1)]

    async def test_buckets_after_last_updated_excluded(self, coordinator, mock_store):
        """Placeholder zeros past the portal's LastUpdated marker are skipped."""
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow().replace(minute=30, second=0, microsecond=0)
        this_hour = now.replace(minute=0)
        last_updated = this_hour - timedelta(hours=3)
        history = [
            {"start": this_hour - timedelta(hours=5), "state": "7.0"},
            {"start": this_hour - timedelta(hours=4), "state": "15.0"},
            {"start": this_hour - timedelta(hours=3), "state": "0.0"},
            {"start": this_hour - timedelta(hours=2), "state": "0.0"},
            {"start": this_hour - timedelta(hours=1), "state": "0.0"},
        ]

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(history, now, last_updated.isoformat())),
        ):
            with patch.object(coordinator, "_import_stats") as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.utcnow", return_value=now):
                    with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                        result = await coordinator._async_update_data()

        assert result["current_sum"] == 22.0
        rows = mock_import.call_args[0][0]
        assert [r["start"] for r in rows] == [
            this_hour - timedelta(hours=5),
            this_hour - timedelta(hours=4),
        ]
        assert [r["sum"] for r in rows] == [7.0, 22.0]

    def test_cutoff_defaults_to_current_hour(self):
        now = datetime(2026, 8, 23, 15, 41, 12, tzinfo=timezone.utc)
        expected = int(datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc).timestamp())
        assert SJWaterHubCoordinator._import_cutoff_ts(now, None) == expected
        assert SJWaterHubCoordinator._import_cutoff_ts(now, "") == expected
        assert SJWaterHubCoordinator._import_cutoff_ts(now, "not a date") == expected
        # A marker in the future never moves the cutoff forward.
        assert SJWaterHubCoordinator._import_cutoff_ts(now, "2099-01-01T00:00:00+00:00") == expected

    def test_cutoff_honours_last_updated_with_offset(self):
        # Real marker from the portal: 05:00-06:00 == 11:00Z.
        now = datetime(2026, 8, 23, 15, 41, 12, tzinfo=timezone.utc)
        expected = int(datetime(2026, 8, 23, 11, 0, tzinfo=timezone.utc).timestamp())
        assert SJWaterHubCoordinator._import_cutoff_ts(now, "2026-08-23T05:00:00-06:00") == expected

    def test_cutoff_treats_naive_marker_as_local(self):
        now = datetime(2026, 8, 23, 15, 41, 12, tzinfo=timezone.utc)
        naive = "2026-08-23T05:00:00"
        expected = int(
            datetime(2026, 8, 23, 5, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            .astimezone(timezone.utc).timestamp()
        )
        assert SJWaterHubCoordinator._import_cutoff_ts(now, naive) == expected


class TestWatermarkRepair:
    async def test_future_watermark_clamped(self, coordinator, mock_store):
        future_ts = int(dt_util.utcnow().timestamp()) + 86400
        mock_store.async_load.return_value = {
            "current_sum": 100.0,
            "last_processed_start": future_ts,
        }

        await coordinator.async_initialize()

        assert coordinator._last_processed_start < int(dt_util.utcnow().timestamp())
        assert coordinator._current_sum == 100.0
        mock_store.async_save.assert_called_once()

    async def test_past_watermark_untouched(self, coordinator, mock_store):
        mock_store.async_load.return_value = {
            "current_sum": 100.0,
            "last_processed_start": 1717000000,
        }

        await coordinator.async_initialize()

        assert coordinator._last_processed_start == 1717000000
        mock_store.async_save.assert_not_called()


class TestImportStats:
    async def test_imports_statistics_correctly(self, coordinator):
        import sys, types

        recorder_models = types.ModuleType("homeassistant.components.recorder.models")
        recorder_models.StatisticMeanType = MagicMock()
        recorder_models.StatisticMeanType.NONE = "NONE"

        recorder_stats = types.ModuleType("homeassistant.components.recorder.statistics")
        recorder_stats.async_add_external_statistics = MagicMock()
        recorder_stats.async_import_statistics = MagicMock()

        sys.modules["homeassistant.components.recorder.models"] = recorder_models
        sys.modules["homeassistant.components.recorder.statistics"] = recorder_stats

        stats = [
            {"start": datetime(2024, 6, 6, 2, 0, 0, tzinfo=timezone.utc), "state": 1.5, "sum": 10.5},
            {"start": datetime(2024, 6, 6, 3, 0, 0, tzinfo=timezone.utc), "state": 2.0, "sum": 12.5},
        ]

        coordinator.client.username = "test@example.com"
        coordinator._import_stats(stats)

        # Must go through the *external* statistics API with a "sjwater:" id,
        # never into the sensor entity's own recorder-compiled series.
        recorder_stats.async_import_statistics.assert_not_called()
        mock_import = recorder_stats.async_add_external_statistics
        mock_import.assert_called_once()
        hass_arg, metadata, rows = mock_import.call_args[0]
        assert hass_arg == coordinator.hass
        assert metadata["statistic_id"] == coordinator.statistic_id
        assert metadata["statistic_id"].startswith("sjwater:")
        assert metadata["source"] == "sjwater"
        assert metadata["has_sum"] is True
        assert metadata["mean_type"] == "NONE"
        assert rows == stats

    async def test_import_stats_exception_swallowed(self, coordinator):
        import sys, types

        recorder_models = types.ModuleType("homeassistant.components.recorder.models")
        recorder_models.StatisticMeanType = MagicMock()
        recorder_models.StatisticMeanType.NONE = "NONE"

        recorder_stats = types.ModuleType("homeassistant.components.recorder.statistics")
        recorder_stats.async_add_external_statistics = MagicMock(
            side_effect=Exception("Import failed")
        )

        sys.modules["homeassistant.components.recorder.models"] = recorder_models
        sys.modules["homeassistant.components.recorder.statistics"] = recorder_stats

        stats = [
            {"start": datetime(2024, 6, 6, 2, 0, 0, tzinfo=timezone.utc), "state": 1.5, "sum": 10.5},
        ]

        coordinator.client.username = "test@example.com"
        coordinator._import_stats(stats)

        recorder_stats.async_add_external_statistics.assert_called_once()


class TestEntityId:
    async def test_generates_correct_entity_id(self, coordinator):
        coordinator.client.username = "test@example.com"

        result = coordinator.entity_id

        assert result.startswith("sensor.sjwater_")
        assert result.endswith("_water_usage")
        assert len(result) > len("sensor.sjwater__water_usage")

    async def test_consistent_across_calls(self, coordinator):
        coordinator.client.username = "test@example.com"

        id1 = coordinator.entity_id
        id2 = coordinator.entity_id

        assert id1 == id2

    async def test_statistic_id_matches_entity_id(self, coordinator):
        coordinator.client.username = "test@example.com"

        # Same account hash and object id as the sensor, under the sjwater: domain.
        assert coordinator.statistic_id == coordinator.entity_id.replace("sensor.sjwater_", "sjwater:")
        assert coordinator.statistic_id == f"sjwater:{coordinator.account_id}_water_usage"
