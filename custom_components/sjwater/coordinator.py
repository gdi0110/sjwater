"""Data update coordinator for SJ Water Hub."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import TYPE_CHECKING

from dateutil.parser import parse as dt_parse

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import SJWaterHubApiClient
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry as SJWaterConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=1)

# The utility publishes hourly readings several hours late and may revise
# them after the fact. Buckets younger than this stay "provisional": they are
# rebuilt and re-imported on every poll (external statistics upsert by hour
# start, so corrections overwrite earlier values) and only buckets older than
# this are finalized into the persisted running sum.
FINALIZATION_LAG = timedelta(hours=48)

# Statistics are written to an *external* statistic id ("sjwater:<acct>_...")
# via async_add_external_statistics rather than into the sensor entity's own
# series. The recorder compiles the entity series itself every hour; writing
# into it for hours the recorder has not finalized yet raced that compile and
# raised "UNIQUE constraint failed: statistics.metadata_id, statistics.start_ts",
# which aborted statistics for *every* entity in Home Assistant (issue #10).
STATISTIC_ID_SUFFIX = "water_usage"


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
    def account_id(self) -> str:
        """Return the short stable non-PII identifier for this account."""
        return hashlib.sha256(self.client.username.encode()).hexdigest()[:8]

    @property
    def entity_id(self) -> str:
        """Return the sensor entity_id for this coordinator's account."""
        return f"sensor.sjwater_{self.account_id}_{STATISTIC_ID_SUFFIX}"

    @property
    def statistic_id(self) -> str:
        """Return the external statistic id the hourly usage is imported to."""
        return f"{DOMAIN}:{self.account_id}_{STATISTIC_ID_SUFFIX}"

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
            import_cutoff_ts = self._import_cutoff_ts(now_utc, api_data.get("last_updated"))
            finalize_cutoff_ts = int((now_utc - FINALIZATION_LAG).timestamp())

            new_sum = self._current_sum or 0.0
            last_start_ts: int | None = None
            total_stats: list[dict] = []
            provisional: list[tuple[int, datetime, float]] = []

            # The API's hourly graph returns per-hour gallons consumed (a delta
            # per bucket), not a cumulative meter reading. Buckets older than
            # FINALIZATION_LAG are folded into the persisted running sum once;
            # younger buckets are set aside as provisional because the utility
            # publishes readings late and may revise them. Buckets at or after
            # the import cutoff (the in-progress hour, and anything the portal
            # has not published yet per LastUpdated) are "0" placeholders the
            # API pads the current day with and must never be imported.
            for entry in history:
                entry_start_dt = entry.get("start")
                entry_state = max(0.0, float(entry.get("state", 0.0)))

                if isinstance(entry_start_dt, datetime):
                    entry_ts = int(entry_start_dt.timestamp())
                else:
                    continue

                if entry_ts >= import_cutoff_ts:
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

    @staticmethod
    def _import_cutoff_ts(now_utc: datetime, last_updated: str | None) -> int:
        """Return the epoch second at/after which hourly buckets are ignored.

        Buckets whose start is at or after the current hour's start are still
        in progress. Buckets at or after the portal's ``LastUpdated`` marker
        have not been published yet and are returned as "0" placeholders.
        Whichever is earlier wins; an unparseable marker falls back to the
        current hour.
        """
        cutoff = now_utc.replace(minute=0, second=0, microsecond=0)
        if last_updated:
            try:
                marker = dt_parse(str(last_updated))
                if marker.tzinfo is None:
                    marker = marker.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                marker = marker.astimezone(timezone.utc)
                if marker < cutoff:
                    cutoff = marker
            except (ValueError, TypeError, OverflowError) as err:
                _LOGGER.debug("Ignoring unparseable LastUpdated %r: %s", last_updated, err)
        return int(cutoff.timestamp())

    def _build_return_data(self, current_sum: float, today_sum: float, latest_timestamp) -> dict:
        """Build the data dict returned to sensor entities."""
        return {
            "current_sum": current_sum,
            "today_sum": today_sum,
            "timestamp": latest_timestamp,
        }

    def _import_stats(self, stats: list[dict]) -> None:
        """Import hourly buckets into the external ``sjwater:`` statistic.

        Finalized rows only ever extend the series (they are past
        ``_last_processed_start``) and provisional rows are upserted on every
        poll, so ``sum`` always continues from the persisted running total —
        preventing the "midnight reset" artifact where a restart re-imported
        the day from sum=0 and clobbered yesterday's accumulated total.
        """
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        metadata = {
            "statistic_id": self.statistic_id,
            "source": DOMAIN,
            "has_sum": True,
            "name": "SJ Water Hub Water Usage",
            "unit_class": VolumeConverter.UNIT_CLASS,
            "unit_of_measurement": UnitOfVolume.GALLONS,
        }
        try:
            # HA 2025.4+ replaced ``has_mean`` with ``mean_type``.
            from homeassistant.components.recorder.models import StatisticMeanType
            metadata["mean_type"] = StatisticMeanType.NONE
        except ImportError:
            metadata["has_mean"] = False

        try:
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug(
                "Imported %d stats (last sum=%.1f)", len(stats), stats[-1]["sum"]
            )
        except Exception as exc:
            _LOGGER.debug("Failed to import statistics: %s", exc)
