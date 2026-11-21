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
from .const import CONF_PRICE_PER_GALLON, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry as SJWaterConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=1)

# Upper bound for adaptive backoff: while the server's LastUpdated marker is
# unchanged, the poll interval doubles each poll up to this cap.
MAX_SCAN_INTERVAL = timedelta(hours=8)

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

# Store flag: the finalized history has been written to the external statistic.
# Absent in state persisted by versions <= 0.0.2, which imported into the
# sensor entity's series instead.
STORE_KEY_EXTERNAL_STATS = "external_stats_backfilled"

# Optional cost statistic ("sjwater:<acct>_water_cost"), written next to the
# usage statistic when a price per gallon is set in the integration options.
# The Energy dashboard only accepts a cost *statistic* for an external usage
# statistic (a fixed price is rejected), so the integration has to supply it.
# Each cost row is priced relative to an anchor: cost_sum = anchor_cost +
# (usage_sum - anchor_usage) * price. A price change moves the anchor to the
# latest row, so history keeps the old price and the cost sum never jumps.
COST_STATISTIC_ID_SUFFIX = "water_cost"
STORE_KEY_COST_PRICE = "cost_price"
STORE_KEY_COST_ANCHOR_USAGE = "cost_anchor_usage"
STORE_KEY_COST_ANCHOR_COST = "cost_anchor_cost"


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
        self._last_import_signature: list | None = None
        self._last_server_update: str | None = None
        # True when state written by a version that imported into the sensor
        # entity's own statistics was restored: the already-finalized history
        # must be copied once into the external statistic.
        self._needs_backfill = False
        price = (entry.options or {}).get(CONF_PRICE_PER_GALLON)
        self._price: float | None = float(price) if price else None
        self._cost_price: float | None = None
        self._cost_anchor_usage = 0.0
        self._cost_anchor_cost = 0.0
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
            self._needs_backfill = (
                self._last_processed_start is not None
                and not stored.get(STORE_KEY_EXTERNAL_STATS)
            )
            self._cost_price = stored.get(STORE_KEY_COST_PRICE)
            self._cost_anchor_usage = stored.get(STORE_KEY_COST_ANCHOR_USAGE, 0.0)
            self._cost_anchor_cost = stored.get(STORE_KEY_COST_ANCHOR_COST, 0.0)
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
            await self._async_save_state()

        if self._price != self._cost_price:
            await self._async_apply_price()

        _LOGGER.debug(
            "State recovery complete: sum=%s, last_processed=%s",
            self._current_sum,
            self._last_processed_start,
        )
        self._initialized = True

    async def _async_save_state(self) -> None:
        """Persist the finalized running sum and watermark."""
        state = {
            "current_sum": self._current_sum,
            "last_processed_start": self._last_processed_start,
            STORE_KEY_EXTERNAL_STATS: not self._needs_backfill,
        }
        if self._cost_price is not None:
            state[STORE_KEY_COST_PRICE] = self._cost_price
            state[STORE_KEY_COST_ANCHOR_USAGE] = self._cost_anchor_usage
            state[STORE_KEY_COST_ANCHOR_COST] = self._cost_anchor_cost
        await self._store.async_save(state)

    async def _async_apply_price(self) -> None:
        """Start, re-anchor, or stop the cost statistic after a price change.

        First price set: price every usage row already in the recorder, so
        the cost history starts where the usage history does. Later change:
        anchor at the latest usage and cost sums so only new rows use the new
        price. Price cleared: stop writing cost; existing rows are kept.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
            statistics_during_period,
        )

        recorder = get_instance(self.hass)
        if self._price is None:
            self._cost_price = None
        elif self._cost_price is None:
            rows = (await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(2000, 1, 1, tzinfo=timezone.utc),
                None,
                {self.statistic_id},
                "hour",
                None,
                {"state", "sum"},
            )).get(self.statistic_id, [])
            self._cost_anchor_usage = 0.0
            self._cost_anchor_cost = 0.0
            self._cost_price = self._price
            if rows:
                usage = [
                    {
                        "start": dt_util.utc_from_timestamp(row["start"]),
                        "state": row.get("state") or 0.0,
                        "sum": row.get("sum") or 0.0,
                    }
                    for row in rows
                ]
                if not self._import_cost_stats(usage):
                    self._cost_price = None
                    return
                _LOGGER.info(
                    "Priced %d hourly readings into %s",
                    len(usage), self.cost_statistic_id,
                )
        else:
            last_usage, last_cost = [
                (await recorder.async_add_executor_job(
                    get_last_statistics, self.hass, 1, stat_id, False, {"sum"}
                )).get(stat_id, [{}])[0].get("sum") or 0.0
                for stat_id in (self.statistic_id, self.cost_statistic_id)
            ]
            self._cost_anchor_usage = last_usage
            self._cost_anchor_cost = last_cost
            self._cost_price = self._price
        await self._async_save_state()

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

    @property
    def cost_statistic_id(self) -> str:
        """Return the external statistic id the hourly cost is imported to."""
        return f"{DOMAIN}:{self.account_id}_{COST_STATISTIC_ID_SUFFIX}"

    async def _async_update_data(self) -> dict:
        """Update data via scraping."""
        # Stamp every successful poll so entities can distinguish "when we
        # last checked" from "when the site's newest reading was" -- the
        # utility publishes readings hours late, so the two often differ.
        polled_at = dt_util.utcnow()
        try:
            # The API fetches data; last_processed_start filters already-seen readings.
            api_data = await self.client.async_get_data("", "", None)
            history = api_data.get("history", [])
            latest_timestamp = api_data.get("timestamp")

            self._adapt_poll_interval(api_data.get("last_updated"))

            if not history:
                _LOGGER.debug("No history returned from API")
                # No readings at all: report no latest data point (the API's
                # timestamp falls back to now() when nothing parsed, which
                # would falsely suggest fresh data).
                return self._build_return_data(self._current_sum or 0.0, 0.0, None, polled_at)

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
            already_finalized: list[tuple[datetime, float]] = []
            for entry in sorted(
                (e for e in history if isinstance(e.get("start"), datetime)),
                key=lambda e: e["start"],
            ):
                entry_start_dt = entry["start"]
                entry_state = max(0.0, float(entry.get("state", 0.0)))
                entry_ts = int(entry_start_dt.timestamp())

                if entry_ts >= import_cutoff_ts:
                    continue

                if entry_ts > finalize_cutoff_ts:
                    provisional.append((entry_ts, entry_start_dt, entry_state))
                    continue

                # Skip already-finalized readings
                if self._last_processed_start is not None and entry_ts <= self._last_processed_start:
                    already_finalized.append((entry_start_dt, entry_state))
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

            # One-time migration from <= 0.0.2: those versions wrote the
            # finalized history into the sensor entity's series, so the
            # external statistic would otherwise start at the watermark and
            # lose everything before it. Rebuild those rows backwards from the
            # persisted running sum so they line up with the rows that follow.
            if self._needs_backfill:
                backfill: list[dict] = []
                running = self._current_sum or 0.0
                for entry_start_dt, entry_state in reversed(already_finalized):
                    backfill.append({
                        "start": entry_start_dt.astimezone(timezone.utc).replace(
                            minute=0, second=0, microsecond=0
                        ),
                        "state": entry_state,
                        "sum": running,
                    })
                    running -= entry_state
                backfill.reverse()
                total_stats = backfill + total_stats
                _LOGGER.info(
                    "Copying %d previously imported hourly readings into %s",
                    len(backfill), self.statistic_id,
                )

            # Rebuild the provisional window on top of the finalized sum every
            # poll. async_add_external_statistics upserts by hour start, so rows for
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

            # The provisional window is rebuilt on top of the finalized sum
            # every poll, so repeat polls within the same hour (restarts,
            # manual refreshes, unchanged API responses) produce an identical
            # batch. Skip the recorder upsert when nothing changed; only
            # record the signature after a successful import so a failed
            # import is retried on the next poll.
            imported = True
            if total_stats:
                signature = [
                    (row["start"], row["state"], row["sum"]) for row in total_stats
                ]
                if signature != self._last_import_signature:
                    imported = self._import_stats(total_stats)
                    if imported:
                        self._last_import_signature = signature

            # Persist state (finalized buckets only), and only once the rows
            # were handed to the recorder: advancing the watermark past rows
            # that were never imported would leave a permanent hole.
            if imported and (last_start_ts is not None or self._needs_backfill):
                self._current_sum = new_sum
                if last_start_ts is not None:
                    self._last_processed_start = last_start_ts
                self._needs_backfill = False
                await self._async_save_state()
                _LOGGER.debug(
                    "Persisted: sum=%s, last_start=%s",
                    self._current_sum, self._last_processed_start,
                )

            # Never report a lower total than a previous poll: a downward
            # provisional revision would otherwise read as a meter reset to
            # the TOTAL_INCREASING sensor, booking phantom consumption.
            if self._last_reported_sum is not None:
                provisional_sum = max(provisional_sum, self._last_reported_sum)
            self._last_reported_sum = provisional_sum

            return self._build_return_data(provisional_sum, today_sum, latest_timestamp, polled_at)

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

    def _build_return_data(self, current_sum: float, today_sum: float, latest_timestamp, polled_at: datetime) -> dict:
        """Build the data dict returned to sensor entities."""
        return {
            "current_sum": current_sum,
            "today_sum": today_sum,
            "timestamp": latest_timestamp,
            "polled_at": polled_at,
        }

    def _adapt_poll_interval(self, last_updated: str | None) -> None:
        """Adapt the poll interval from the server's LastUpdated marker.

        The utility only has new readings when its own data changes, which
        the API exposes via LastUpdated. While the marker is unchanged the
        poll interval doubles each poll (up to MAX_SCAN_INTERVAL); it resets
        to SCAN_INTERVAL as soon as the marker moves. Without a marker we
        cannot detect server-side changes, so the base interval is kept.
        """
        if not last_updated:
            self._last_server_update = None
            if self.update_interval != SCAN_INTERVAL:
                _LOGGER.debug(
                    "No LastUpdated marker; resetting poll interval to %s",
                    SCAN_INTERVAL,
                )
                self.update_interval = SCAN_INTERVAL
            return

        if last_updated != self._last_server_update:
            self._last_server_update = last_updated
            if self.update_interval != SCAN_INTERVAL:
                _LOGGER.debug(
                    "Server data changed; resetting poll interval to %s",
                    SCAN_INTERVAL,
                )
                self.update_interval = SCAN_INTERVAL
            return

        new_interval = min(self.update_interval * 2, MAX_SCAN_INTERVAL)
        if new_interval != self.update_interval:
            _LOGGER.debug(
                "Server data unchanged; backing off poll interval to %s",
                new_interval,
            )
            self.update_interval = new_interval

    def _import_stats(self, stats: list[dict]) -> bool:
        """Import hourly buckets into the external ``sjwater:`` statistic.

        Finalized rows only ever extend the series (they are past
        ``_last_processed_start``) and provisional rows are upserted on every
        poll, so ``sum`` always continues from the persisted running total —
        preventing the "midnight reset" artifact where a restart re-imported
        the day from sum=0 and clobbered yesterday's accumulated total.

        Returns True when the rows were handed to the recorder, False when
        the import raised, so the caller can retry the same batch on the
        next poll instead of treating it as imported.
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
        except Exception as exc:  # noqa: BLE001 - never let stats break the poll
            _LOGGER.warning(
                "Failed to import statistics into %s: %s", self.statistic_id, exc
            )
            return False
        if self._cost_price is not None:
            self._import_cost_stats(stats)
        return True

    def _import_cost_stats(self, usage: list[dict]) -> bool:
        """Import the cost of the given usage rows into the cost statistic."""
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        price = self._cost_price or 0.0
        rows = [
            {
                "start": row["start"],
                "state": row["state"] * price,
                "sum": self._cost_anchor_cost
                + (row["sum"] - self._cost_anchor_usage) * price,
            }
            for row in usage
        ]
        metadata = {
            "statistic_id": self.cost_statistic_id,
            "source": DOMAIN,
            "has_sum": True,
            "name": "SJ Water Hub Water Cost",
            "unit_class": None,
            "unit_of_measurement": None,  # same as core Opower cost statistics
        }
        try:
            from homeassistant.components.recorder.models import StatisticMeanType
            metadata["mean_type"] = StatisticMeanType.NONE
        except ImportError:
            metadata["has_mean"] = False

        try:
            async_add_external_statistics(self.hass, metadata, rows)
            return True
        except Exception as exc:  # noqa: BLE001 - never let stats break the poll
            _LOGGER.warning(
                "Failed to import statistics into %s: %s", self.cost_statistic_id, exc
            )
            return False
