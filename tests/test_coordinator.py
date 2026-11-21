from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.sjwater.coordinator import (
    MAX_SCAN_INTERVAL,
    SCAN_INTERVAL,
    SJWaterHubCoordinator,
)

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
        assert "polled_at" in result
        assert result["timestamp"] == now
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
        # No readings returned -> no latest data point, but the poll time is
        # still recorded so entities can show freshness.
        assert result["timestamp"] is None
        assert result["polled_at"] is not None
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
            "external_stats_backfilled": True,
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
            "external_stats_backfilled": True,
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


class TestStatsImportDedup:
    async def test_skips_import_when_batch_unchanged(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        # Provisional entries (younger than FINALIZATION_LAG)
        history = [
            {"start": now - timedelta(hours=2), "state": "1.5"},
            {"start": now - timedelta(hours=1), "state": "2.5"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.5,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()
                    await coordinator._async_update_data()

        assert mock_import.call_count == 1

    async def test_reimports_when_batch_changes(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=2), "state": "1.5"},
            {"start": now - timedelta(hours=1), "state": "2.5"},
        ]
        revised_history = history + [
            {"start": now - timedelta(hours=1), "state": "3.0"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(side_effect=[
            {
                "gallons": 2.5,
                "timestamp": now,
                "last_updated": PUBLISHED_EVERYTHING,
                "history": history,
            },
            {
                "gallons": 3.0,
                "timestamp": now,
                "last_updated": "2099-01-02T00:00:00+00:00",
                "history": revised_history,
            },
        ])):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()
                    await coordinator._async_update_data()

        assert mock_import.call_count == 2

    async def test_retries_when_import_fails(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        history = [
            {"start": now - timedelta(hours=1), "state": "2.5"},
        ]

        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 2.5,
            "timestamp": now,
            "last_updated": PUBLISHED_EVERYTHING,
            "history": history,
        })):
            with patch.object(coordinator, "_import_stats", return_value=False) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()
                    await coordinator._async_update_data()

        # Failed imports are not recorded as imported, so the next poll
        # with an identical batch retries the import.
        assert mock_import.call_count == 2


class TestAdaptPollInterval:
    def test_backs_off_when_marker_unchanged(self, coordinator):
        coordinator._adapt_poll_interval("2024-06-06T12:00:00")  # first sighting
        assert coordinator.update_interval == SCAN_INTERVAL

        coordinator._adapt_poll_interval("2024-06-06T12:00:00")
        assert coordinator.update_interval == SCAN_INTERVAL * 2

        coordinator._adapt_poll_interval("2024-06-06T12:00:00")
        assert coordinator.update_interval == SCAN_INTERVAL * 4

    def test_backoff_capped_at_max(self, coordinator):
        coordinator._last_server_update = "2024-06-06T12:00:00"

        for _ in range(10):
            coordinator._adapt_poll_interval("2024-06-06T12:00:00")

        assert coordinator.update_interval == MAX_SCAN_INTERVAL

    def test_resets_when_marker_changes(self, coordinator):
        coordinator._last_server_update = "2024-06-06T12:00:00"
        coordinator.update_interval = MAX_SCAN_INTERVAL

        coordinator._adapt_poll_interval("2024-06-07T12:00:00")

        assert coordinator.update_interval == SCAN_INTERVAL
        assert coordinator._last_server_update == "2024-06-07T12:00:00"

    def test_keeps_base_interval_without_marker(self, coordinator):
        coordinator._last_server_update = "2024-06-06T12:00:00"
        coordinator.update_interval = MAX_SCAN_INTERVAL

        coordinator._adapt_poll_interval("")

        assert coordinator.update_interval == SCAN_INTERVAL
        assert coordinator._last_server_update is None

    async def test_update_data_triggers_adaptation(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None

        now = dt_util.utcnow()
        with patch.object(coordinator.client, "async_get_data", new=AsyncMock(return_value={
            "gallons": 0,
            "timestamp": now,
            "last_updated": "2024-06-06T12:00:00",
            "history": [],
        })):
            with patch.object(coordinator, "_import_stats"):
                await coordinator._async_update_data()
                assert coordinator.update_interval == SCAN_INTERVAL

                await coordinator._async_update_data()
                assert coordinator.update_interval == SCAN_INTERVAL * 2


class TestExternalStatsBackfill:
    """Upgrade path from <= 0.0.2, which imported into the entity's series."""

    @staticmethod
    def _history(now):
        base = (now - timedelta(hours=100)).replace(minute=0, second=0, microsecond=0)
        return base, [
            {"start": base, "state": "1.0"},
            {"start": base + timedelta(hours=1), "state": "2.0"},
            {"start": base + timedelta(hours=2), "state": "3.0"},
            {"start": base + timedelta(hours=3), "state": "4.0"},
        ]

    async def test_legacy_state_backfills_finalized_history(self, coordinator, mock_store):
        now = dt_util.utcnow()
        base, history = self._history(now)
        # Legacy store: first three hours already folded into the sum.
        mock_store.async_load.return_value = {
            "current_sum": 6.0,
            "last_processed_start": int((base + timedelta(hours=2)).timestamp()),
        }
        await coordinator.async_initialize()
        assert coordinator._needs_backfill is True

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(history, now)),
        ):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    result = await coordinator._async_update_data()

        rows = mock_import.call_args[0][0]
        assert [r["start"] for r in rows] == [h["start"] for h in history]
        assert [r["state"] for r in rows] == [1.0, 2.0, 3.0, 4.0]
        # Sums are continuous and end on the persisted running total.
        assert [r["sum"] for r in rows] == [1.0, 3.0, 6.0, 10.0]
        assert result["current_sum"] == 10.0
        assert coordinator._needs_backfill is False
        mock_store.async_save.assert_called_once_with({
            "current_sum": 10.0,
            "last_processed_start": int((base + timedelta(hours=3)).timestamp()),
            "external_stats_backfilled": True,
        })

    async def test_backfill_keeps_sum_continuous_when_history_exceeds_sum(
        self, coordinator, mock_store
    ):
        now = dt_util.utcnow()
        base, history = self._history(now)
        # Legacy sum lower than the portal's history (earlier versions could
        # drop readings): rows must still line up with the persisted sum.
        mock_store.async_load.return_value = {
            "current_sum": 5.0,
            "last_processed_start": int((base + timedelta(hours=3)).timestamp()),
        }
        await coordinator.async_initialize()

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(history, now)),
        ):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()

        rows = mock_import.call_args[0][0]
        assert [r["sum"] for r in rows] == [-4.0, -2.0, 1.0, 5.0]
        assert coordinator._current_sum == 5.0
        mock_store.async_save.assert_called_once()

    async def test_backfill_retried_when_import_fails(self, coordinator, mock_store):
        now = dt_util.utcnow()
        base, history = self._history(now)
        watermark = int((base + timedelta(hours=2)).timestamp())
        mock_store.async_load.return_value = {
            "current_sum": 6.0,
            "last_processed_start": watermark,
        }
        await coordinator.async_initialize()

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(history, now)),
        ):
            with patch.object(coordinator, "_import_stats", return_value=False) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()
                    await coordinator._async_update_data()

        # Nothing persisted, watermark not advanced, full batch retried.
        assert mock_import.call_count == 2
        assert len(mock_import.call_args[0][0]) == 4
        assert coordinator._needs_backfill is True
        assert coordinator._last_processed_start == watermark
        assert coordinator._current_sum == 6.0
        mock_store.async_save.assert_not_called()

    async def test_migrated_state_does_not_backfill(self, coordinator, mock_store):
        now = dt_util.utcnow()
        base, history = self._history(now)
        mock_store.async_load.return_value = {
            "current_sum": 6.0,
            "last_processed_start": int((base + timedelta(hours=2)).timestamp()),
            "external_stats_backfilled": True,
        }
        await coordinator.async_initialize()
        assert coordinator._needs_backfill is False

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(history, now)),
        ):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()

        rows = mock_import.call_args[0][0]
        assert [r["sum"] for r in rows] == [10.0]

    async def test_fresh_install_needs_no_backfill(self, coordinator, mock_store):
        mock_store.async_load.return_value = None
        await coordinator.async_initialize()
        assert coordinator._needs_backfill is False

    async def test_unsorted_history_is_processed_in_order(self, coordinator, mock_store):
        coordinator._current_sum = 0.0
        coordinator._last_processed_start = None
        now = dt_util.utcnow()
        _, history = self._history(now)

        with patch.object(
            coordinator.client, "async_get_data",
            new=AsyncMock(return_value=_api_data(list(reversed(history)), now)),
        ):
            with patch.object(coordinator, "_import_stats", return_value=True) as mock_import:
                with patch("custom_components.sjwater.coordinator.dt_util.now", return_value=now):
                    await coordinator._async_update_data()

        rows = mock_import.call_args[0][0]
        assert [r["sum"] for r in rows] == [1.0, 3.0, 6.0, 10.0]
        assert coordinator._last_processed_start == int(history[-1]["start"].timestamp())


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


def _fake_recorder(usage_rows=None, last_sums=None, fail_cost=False):
    """Install fake recorder modules; return the external-statistics mock."""
    import sys, types

    recorder_models = types.ModuleType("homeassistant.components.recorder.models")
    recorder_models.StatisticMeanType = MagicMock()
    recorder_models.StatisticMeanType.NONE = "NONE"

    def add_external(hass, metadata, rows):
        if fail_cost and metadata["statistic_id"].endswith("_water_cost"):
            raise Exception("Import failed")

    recorder_stats = types.ModuleType("homeassistant.components.recorder.statistics")
    recorder_stats.async_add_external_statistics = MagicMock(side_effect=add_external)
    recorder_stats.statistics_during_period = MagicMock(
        side_effect=lambda hass, start, end, ids, *a: {
            stat_id: list(usage_rows or []) for stat_id in ids
        }
    )
    recorder_stats.get_last_statistics = MagicMock(
        side_effect=lambda hass, n, stat_id, *a: (
            {stat_id: [{"sum": last_sums[stat_id]}]}
            if last_sums and stat_id in last_sums else {}
        )
    )

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    import homeassistant.components.recorder as recorder_pkg
    sys.modules["homeassistant.components.recorder.models"] = recorder_models
    sys.modules["homeassistant.components.recorder.statistics"] = recorder_stats
    return recorder_stats, patch.object(
        recorder_pkg, "get_instance", return_value=instance, create=True
    )


def _calls_for(mock_import, stat_id):
    return [c[0][2] for c in mock_import.call_args_list if c[0][1]["statistic_id"] == stat_id]


class TestWaterCost:
    STATS = [
        {"start": datetime(2024, 6, 6, 2, 0, 0, tzinfo=timezone.utc), "state": 10.0, "sum": 110.0},
        {"start": datetime(2024, 6, 6, 3, 0, 0, tzinfo=timezone.utc), "state": 20.0, "sum": 130.0},
    ]

    async def test_no_cost_without_price(self, coordinator):
        recorder_stats, _ = _fake_recorder()
        coordinator._import_stats(self.STATS)
        assert _calls_for(recorder_stats.async_add_external_statistics, coordinator.cost_statistic_id) == []

    async def test_cost_rows_follow_usage_rows(self, coordinator):
        recorder_stats, _ = _fake_recorder()
        coordinator._cost_price = 0.01
        coordinator._cost_anchor_usage = 100.0
        coordinator._cost_anchor_cost = 5.0

        assert coordinator._import_stats(self.STATS) is True

        mock_import = recorder_stats.async_add_external_statistics
        (rows,) = _calls_for(mock_import, coordinator.cost_statistic_id)
        assert [r["start"] for r in rows] == [r["start"] for r in self.STATS]
        assert [r["state"] for r in rows] == pytest.approx([0.1, 0.2])
        assert [r["sum"] for r in rows] == pytest.approx([5.1, 5.3])
        metadata = next(
            c[0][1] for c in mock_import.call_args_list
            if c[0][1]["statistic_id"] == coordinator.cost_statistic_id
        )
        assert metadata["statistic_id"].startswith("sjwater:")
        assert metadata["has_sum"] is True
        assert metadata["unit_of_measurement"] is None

    async def test_cost_failure_does_not_fail_usage_import(self, coordinator):
        _fake_recorder(fail_cost=True)
        coordinator._cost_price = 0.01
        assert coordinator._import_stats(self.STATS) is True

    async def test_first_price_prices_existing_history(self, coordinator, mock_store):
        usage = [
            {"start": r["start"].timestamp(), "state": r["state"], "sum": r["sum"]}
            for r in self.STATS
        ]
        recorder_stats, get_instance = _fake_recorder(usage_rows=usage)
        coordinator._price = 0.01

        with get_instance:
            await coordinator.async_initialize()

        (rows,) = _calls_for(recorder_stats.async_add_external_statistics, coordinator.cost_statistic_id)
        assert [r["start"] for r in rows] == [r["start"] for r in self.STATS]
        assert [r["sum"] for r in rows] == pytest.approx([1.1, 1.3])
        saved = mock_store.async_save.call_args[0][0]
        assert saved["cost_price"] == 0.01
        assert saved["cost_anchor_usage"] == 0.0
        assert saved["cost_anchor_cost"] == 0.0

    async def test_first_price_retried_when_history_import_fails(self, coordinator, mock_store):
        usage = [{"start": self.STATS[0]["start"].timestamp(), "state": 10.0, "sum": 110.0}]
        _, get_instance = _fake_recorder(usage_rows=usage, fail_cost=True)
        coordinator._price = 0.01

        with get_instance:
            await coordinator.async_initialize()

        assert coordinator._cost_price is None
        mock_store.async_save.assert_not_called()

    async def test_price_change_anchors_at_latest_sums(self, coordinator, mock_store):
        mock_store.async_load.return_value = {
            "current_sum": 130.0,
            "last_processed_start": 1,
            "external_stats_backfilled": True,
            "cost_price": 0.01,
            "cost_anchor_usage": 0.0,
            "cost_anchor_cost": 0.0,
        }
        recorder_stats, get_instance = _fake_recorder(last_sums={
            coordinator.statistic_id: 130.0,
            coordinator.cost_statistic_id: 1.3,
        })
        coordinator._price = 0.02

        with get_instance:
            await coordinator.async_initialize()

        assert coordinator._cost_price == 0.02
        assert coordinator._cost_anchor_usage == 130.0
        assert coordinator._cost_anchor_cost == 1.3
        recorder_stats.statistics_during_period.assert_not_called()

        coordinator._import_stats([
            {"start": datetime(2024, 6, 6, 4, 0, 0, tzinfo=timezone.utc), "state": 10.0, "sum": 140.0},
        ])
        (rows,) = _calls_for(recorder_stats.async_add_external_statistics, coordinator.cost_statistic_id)
        assert rows[0]["sum"] == pytest.approx(1.5)

    async def test_clearing_price_stops_cost(self, coordinator, mock_store):
        mock_store.async_load.return_value = {
            "current_sum": 130.0,
            "last_processed_start": 1,
            "external_stats_backfilled": True,
            "cost_price": 0.01,
        }
        recorder_stats, get_instance = _fake_recorder()

        with get_instance:
            await coordinator.async_initialize()

        assert coordinator._cost_price is None
        assert "cost_price" not in mock_store.async_save.call_args[0][0]
        coordinator._import_stats(self.STATS)
        assert _calls_for(recorder_stats.async_add_external_statistics, coordinator.cost_statistic_id) == []

    async def test_unchanged_price_does_nothing(self, coordinator, mock_store):
        mock_store.async_load.return_value = {
            "current_sum": 130.0,
            "last_processed_start": 1,
            "external_stats_backfilled": True,
            "cost_price": 0.01,
        }
        recorder_stats, get_instance = _fake_recorder()
        coordinator._price = 0.01

        with get_instance:
            await coordinator.async_initialize()

        recorder_stats.statistics_during_period.assert_not_called()
        recorder_stats.get_last_statistics.assert_not_called()
        mock_store.async_save.assert_not_called()
