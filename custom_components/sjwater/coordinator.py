"""Data update coordinator for SJ Water Hub."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import TYPE_CHECKING

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import SJWaterHubApiClient
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.config_entries import ConfigEntry as SJWaterConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=4)

# The utility publishes hourly readings 6-24h late and may revise them after
# the fact; the API pads the current day with 0.0 placeholder rows for hours
# that have not elapsed yet. Buckets younger than this stay "provisional":
# they are rebuilt and re-imported on every poll (statistics import upserts
# by hour start, so corrections overwrite placeholders) and only buckets
# older than this are finalized into the persisted running sum.
FINALIZATION_LAG = timedelta(hours=48)


class SJWaterHubCoordinator(DataUpdateCoordinator):
    """Coordinator for SJ Water Hub data fetching and state persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SJWaterConfigEntry,
        client: SJWaterHubApiClient,
    ) -> None:
        import inspect
        init_sig = inspect.signature(DataUpdateCoordinator.__init__)
        kwargs = dict(
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            update_method=self._async_update_data,
        )
        if "config_entry" in init_sig.parameters:
            kwargs["config_entry"] = entry
        super().__init__(
            hass,
            _LOGGER,
            **kwargs,
        )
        self.entry = entry
        self.client = client
        self._store = Store(
            hass,
            version=1,
            key=f"{DOMAIN}_state_{entry.entry_id}",
            encoder=json.JSONEncoder,
        )
        self._current_sum: float | None = None
        self._last_processed_start: int | None = None
        self._last_reported_sum: float | None = None
        self._initialized = False
        _LOGGER.debug("%s: Coordinator created", DOMAIN)

    async def async_initialize(self) -> None:
        """Restore persisted state before first refresh."""
        _LOGGER.debug("Restoring persisted state...")

        # Try restoring from disk store
        stored = await self._store.async_load()
        if stored:
            self._current_sum = stored.get("current_sum")
            self._last_processed_start = stored.get("last_processed_start")
            _LOGGER.debug(
                "Restored from store: current_sum=%s, last_processed_start=%s",
                self._current_sum,
                self._last_processed_start,
            )

        # Repair a watermark that ran into the future. Earlier versions
        # processed the API's 0.0 placeholder rows for not-yet-elapsed hours,
        # advancing the watermark to end-of-day and permanently blocking the
        # real readings that arrive later. Every bucket in the rewound range
        # contributed exactly 0.0 to the sum, so no double-counting occurs
        # when those hours are re-processed.
        now_ts = int(dt_util.utcnow().timestamp())
        if self._last_processed_start is not None and self._last_processed_start > now_ts:
            clamped = int((dt_util.utcnow() - FINALIZATION_LAG).timestamp())
            _LOGGER.info(
                "Repairing future watermark: %s -> %s",
                self._last_processed_start,
                clamped,
            )
            self._last_processed_start = clamped
            await self._store.async_save({
                "current_sum": self._current_sum,
                "last_processed_start": self._last_processed_start,
            })

        _LOGGER.debug(
            "State recovery complete: sum=%s, last_processed=%s",
            self._current_sum,
            self._last_processed_start,
        )
        self._initialized = True

    @property
    def entity_id(self) -> str:
        """Return the sensor entity_id for this coordinator's account."""
        import hashlib
        acct = hashlib.sha256(self.client.username.encode()).hexdigest()[:8]
        return f"sensor.sjwater_{acct}_water_usage"

    async def _async_update_data(self) -> dict:
        """Update data via scraping."""
        try:
            # The API fetches data; last_processed_start filters already-seen readings.
            api_data = await self.client.async_get_data("", "", None)
            history = api_data.get("history", [])
            latest_timestamp = api_data.get("timestamp")

            if not history:
                _LOGGER.debug("No history returned from API")
                return self._build_return_data(self._current_sum or 0.0, 0.0, latest_timestamp)

            _LOGGER.debug("API returned %d history entries", len(history))

            now = dt_util.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # Compute today's total from ALL history entries (not just newly-processed)
            today_sum = 0.0
            for entry in history:
                entry_start_dt = entry.get("start")
                if isinstance(entry_start_dt, datetime):
                    entry_local = dt_util.as_local(entry_start_dt)
                    if entry_local.date() == today_start.date():
                        today_sum += max(0.0, float(entry.get("state", 0.0)))

            now_utc = dt_util.utcnow()
            now_ts = int(now_utc.timestamp())
            finalize_cutoff_ts = int((now_utc - FINALIZATION_LAG).timestamp())

            new_sum = self._current_sum or 0.0
            last_start_ts: int | None = None
            total_stats: list[dict] = []
            provisional: list[tuple[int, datetime, float]] = []

            # The API's hourly graph returns per-hour gallons consumed (a delta
            # per bucket), not a cumulative meter reading. Buckets older than
            # FINALIZATION_LAG are folded into the persisted running sum once;
            # younger buckets are set aside as provisional because the utility
            # publishes readings late and may revise them. Buckets in the
            # future are placeholders the API pads the current day with.
            for entry in history:
                entry_start_dt = entry.get("start")
                entry_state = max(0.0, float(entry.get("state", 0.0)))

                if isinstance(entry_start_dt, datetime):
                    entry_ts = int(entry_start_dt.timestamp())
                else:
                    continue

                if entry_ts > now_ts:
                    continue

                if entry_ts > finalize_cutoff_ts:
                    provisional.append((entry_ts, entry_start_dt, entry_state))
                    continue

                # Skip already-finalized readings
                if self._last_processed_start is not None and entry_ts <= self._last_processed_start:
                    continue

                new_sum += entry_state
                last_start_ts = entry_ts

                utc_start = entry_start_dt.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )

                # Queue a statistics row for this hour so the Energy Dashboard
                # stays in sync with the live sensor's running total.
                total_stats.append({
                    "start": utc_start,
                    "state": entry_state,
                    "sum": new_sum,
                })

                _LOGGER.debug(
                    "Finalized reading: ts=%s, hourly_gal=%s, running_sum=%s",
                    entry_ts, entry_state, new_sum,
                )

            # Persist state (finalized buckets only)
            if last_start_ts is not None:
                self._current_sum = new_sum
                self._last_processed_start = last_start_ts
                await self._store.async_save({
                    "current_sum": self._current_sum,
                    "last_processed_start": self._last_processed_start,
                })
                _LOGGER.debug("Persisted: sum=%s, last_start=%s", new_sum, last_start_ts)

            # Rebuild the provisional window on top of the finalized sum every
            # poll. async_import_statistics upserts by hour start, so rows for
            # hours whose readings arrived or were revised since the last poll
            # overwrite the stale placeholder rows in the statistics table.
            provisional_sum = new_sum
            for entry_ts, entry_start_dt, entry_state in sorted(
                provisional, key=lambda e: e[0]
            ):
                provisional_sum += entry_state
                utc_start = entry_start_dt.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                total_stats.append({
                    "start": utc_start,
                    "state": entry_state,
                    "sum": provisional_sum,
                })
            if provisional:
                _LOGGER.debug(
                    "Rebuilt %d provisional rows (provisional_sum=%.1f)",
                    len(provisional), provisional_sum,
                )

            if total_stats:
                self._import_stats(total_stats)

            # Never report a lower total than a previous poll: a downward
            # provisional revision would otherwise read as a meter reset to
            # the TOTAL_INCREASING sensor, booking phantom consumption.
            if self._last_reported_sum is not None:
                provisional_sum = max(provisional_sum, self._last_reported_sum)
            self._last_reported_sum = provisional_sum

            return self._build_return_data(provisional_sum, today_sum, latest_timestamp)

        except Exception as exc:
            _LOGGER.warning("Error fetching water data: %s", exc)
            raise

    def _build_return_data(self, current_sum: float, today_sum: float, latest_timestamp) -> dict:
        """Build the data dict returned to sensor entities."""
        return {
            "current_sum": current_sum,
            "today_sum": today_sum,
            "timestamp": latest_timestamp,
        }

    def _import_stats(self, stats: list[dict]) -> None:
        """Import statistics rows for newly-seen hourly buckets.

        Only rows past ``_last_processed_start`` reach this point, so the
        ``sum`` values always extend the existing series — preventing the
        "midnight reset" artifact where a restart re-imported the day from
        sum=0 and clobbered yesterday's accumulated total.
        """
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import async_import_statistics
        try:
            async_import_statistics(
                self.hass,
                metadata={
                    "statistic_id": self.entity_id,
                    "source": "recorder",
                    "mean_type": StatisticMeanType.NONE,
                    "has_sum": True,
                    "name": "Water Meter Total",
                    "unit_class": VolumeConverter.UNIT_CLASS,
                    "unit_of_measurement": UnitOfVolume.GALLONS,
                },
                statistics=stats,
            )
            _LOGGER.debug(
                "Imported %d stats (last sum=%.1f)", len(stats), stats[-1]["sum"]
            )
        except Exception as exc:
            _LOGGER.debug("Failed to import statistics: %s", exc)
