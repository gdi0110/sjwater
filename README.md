<p align="center">
  <img src="brand/logo.png" alt="Logo" height="150" valign="middle">
</p>

Custom Home Assistant integration for **SJ Water Company** (sjwaterhub.com). Fetches hourly water usage data from the water company's web portal and exposes it as Home Assistant sensors with full Energy Dashboard support.

## Features

- **Hourly water usage** — Scrapes the SJ Water Hub portal API for per-hour consumption data (in gallons)
- **Two sensors per account:**
  - **Water Meter Total** (`sensor.sjwater_<id>_water_usage`) — Monotonically increasing total for dashboards and automations (for the Energy Dashboard, select the **SJ Water Hub Water Usage** statistic instead; see [Energy Dashboard Setup](#energy-dashboard-setup))
  - **Today's Water Usage** (`sensor.sjwater_<id>_todays_water_usage`) — Daily reset counter showing current day's consumption
- **Energy Dashboard compatible** — Imports hourly usage into an external statistic (`sjwater:<id>_water_usage`) so the water meter appears alongside gas/electric usage, with readings landing in the hour they were actually consumed
- **Persistent state** — Running sum survives HA restarts via `homeassistant.helpers.storage.Store`
- **Session management** — Auto-authenticates, handles token rotation, and re-authenticates on session expiry
- **Config flow** — UI-based setup via Settings → Devices & Services

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS (if not already listed)
2. Search for **"SJ Water Hub"** in HACS and install
3. Restart Home Assistant

### Manual

1. Copy the `sjwater/` folder into your `custom_components/` directory
2. Restart Home Assistant

## Configuration

### Via UI (Config Flow)

1. Go to **Settings → Devices & Services**
2. Click **Add Integration**
3. Search for **"SJ Water Hub"**
4. Enter your SJ Water Hub **username** and **password**
5. Submit — the integration will verify credentials and set up automatically

### Multiple Accounts

Each account configuration creates a unique ID based on a hash of the username, so you can add multiple water accounts if needed. The integration prevents duplicate configurations for the same account.

## Sensors

| Sensor | Entity ID Pattern | State Class | Description |
|--------|-------------------|-------------|-------------|
| Water Meter Total | `sensor.sjwater_<hash>_water_usage` | `TOTAL_INCREASING` | Cumulative water consumption (gallons). Never decreases — any drop is treated as a counter reset. |
| Today's Water Usage | `sensor.sjwater_<hash>_todays_water_usage` | `TOTAL` | Water used today (gallons). Resets at midnight with `last_reset` set to start of day. |

The `<hash>` is the first 8 characters of SHA-256 of the username — a stable, non-PII identifier.

### Attributes

Both sensors include two freshness attributes:

| Attribute | Description |
|-----------|-------------|
| `last_polled_at` | When the integration last successfully polled the SJ Water Hub portal. |
| `latest_data_point_at` | Timestamp of the most recent reading the portal actually had (`None` if no readings were returned). |

The utility publishes hourly readings 6–24 hours late, so these routinely differ: e.g. a poll at 11am (`last_polled_at`) may find the newest reading (`latest_data_point_at`) is from yesterday evening. On a failed poll the previous data is retained, so `last_polled_at` reflects the last *successful* poll.

## Energy Dashboard Setup

1. Go to **Settings → Dashboards → Energy**
2. Under **Water consumption**, click **Add consumption**
3. In the list, select **SJ Water Hub Water Usage**
4. Save. Historical data populates immediately because the integration imports hourly statistics

The integration provides three water items, and only one belongs in the Energy dashboard:

| Name shown in the list | ID | Use in Energy dashboard? |
|------------------------|----|--------------------------|
| **SJ Water Hub Water Usage** | `sjwater:<hash>_water_usage` (statistic) | **Yes, select this one** |
| SJ Water Hub Water Meter Total | `sensor.sjwater_<hash>_water_usage` (sensor) | No |
| SJ Water Hub Today's Water Usage | `sensor.sjwater_<hash>_todays_water_usage` (sensor) | No |

The two `_water_usage` IDs differ only in their prefix: the correct one starts with `sjwater:` (a colon), the wrong one with `sensor.`. To find your `<hash>`, open **Developer Tools → Statistics** and search for `sjwater:`.

The portal publishes readings several hours late. Writing them into the sensor entity's own
statistics would place them in the hour they were *fetched*, and it also raced the recorder's
hourly compile of that same series (`UNIQUE constraint failed: statistics.metadata_id,
statistics.start_ts`, which stalls statistics for every entity in Home Assistant). The external
statistic avoids both: the recorder never writes to it, and each hourly bucket is stored at the
hour it was consumed.

> **Upgrading from 0.0.2 or earlier:** open the Energy dashboard settings, remove
> **SJ Water Hub Water Meter Total** (`sensor.sjwater_<hash>_water_usage`) from Water consumption,
> and add **SJ Water Hub Water Usage** (`sjwater:<hash>_water_usage`) instead. On the
> first poll after the upgrade the integration copies the history it had already imported
> into the new statistic, so nothing is lost. Versions up to 0.0.2 could abort the recorder's
> hourly statistics run for *every* entity (issues #10 and #12); hours that were dropped for
> other sensors while those versions were installed cannot be recovered, and one last
> `UNIQUE constraint failed` error may be logged at the top of the hour in which you upgrade.

### Water cost (optional)

The Energy dashboard won't apply a fixed price to an imported statistic like **SJ Water Hub Water Usage**, so the integration can write a matching cost statistic for you:

1. Go to **Settings → Devices & services → SJ Water Hub → Configure**
2. Enter your **price per gallon**, including any per-unit surcharges and taxes (SJ Water bills per CCF; 1 CCF = 748 gallons)
3. In the Energy dashboard's water settings, choose **Use an entity tracking the total costs** and select **SJ Water Hub Water Cost**

The first time you set a price, the integration prices all the usage history it has already imported. If you change the price later, the new price applies from then on and earlier hours keep the price they were recorded with. Clear the field to stop writing cost.

The cost is a flat rate, so it won't follow SJ Water's monthly tiers, and it leaves out the fixed service charge. The portal also reports usage in steps of about 7.5 gallons (one cubic foot, rounded to whole gallons), so each hour's cost moves in steps of the same size.

## How It Works

### Authentication

The integration authenticates against `sjwaterhub.com` using a three-step process:

1. **GET** the login page to extract an anti-forgery token from a hidden `<input id="Token">` field
2. **POST** that token to `/api/WebApi/CreateExceptionPermissions` (the portal rejects logins that skip this)
3. **POST** credentials via the `VXengage_Login` action through the `RequestBroker` API

The portal keeps no session cookie; the only cookies are load-balancer affinity cookies. Every
`RequestBroker` response returns a fresh `Token`, which is stored and sent with the next call.
If the session expires, the integration automatically re-authenticates.

### Data Fetching

The coordinator polls `VXengage_GetHourlyGraph` every **1 hour** (`SCAN_INTERVAL = timedelta(hours=1)`). Each fetch returns:

- Per-hour water consumption in gallons (delta values, not cumulative). The meter reports in
  1 ft³ increments, so values are multiples of ~7.48 gal.
- Timestamps for each reading (local time, converted to UTC for HA)
- Last-updated timestamp (`LastUpdated`)

The portal pads the current day with `"0"` rows for hours it has not published yet. Any bucket
starting at or after the current hour, or at or after `LastUpdated`, is ignored; those hours are
picked up on a later poll once real readings exist.

**Adaptive polling:** the API's `LastUpdated` marker only moves when the utility publishes new data. While the marker is unchanged between polls, the coordinator doubles its poll interval each cycle (up to **8 hours**); it resets to 1 hour as soon as new data appears. This avoids hammering the portal during the long stretches when nothing has changed.

New readings are tracked via `_last_processed_start` to avoid duplicate imports. The running sum (`_current_sum`) is persisted to disk so it survives restarts. Identical statistics batches (e.g., repeat polls within the same hour) are detected and skipped to avoid redundant recorder writes.

### State Persistence

The coordinator maintains a running total across restarts using `Store` — Home Assistant's key-value storage, persisting `current_sum` and `last_processed_start`.

This prevents the "midnight reset" artifact where a restart could re-import the day's data from `sum=0` and clobber the accumulated total.

## Security & Privacy

- **Credential storage:** Your SJ Water Hub username and password are stored as plaintext in Home Assistant's config entry storage (`.storage/core.config_entries`). This is inherent to how HA integrations persist credentials — there is no secrets API for config entries. Because HA backups include `.storage`, anyone with access to your config directory or backup archives can read them. **Use a unique password for your SJ Water Hub account** — not one you reuse anywhere else.
- **Session tokens** are kept in memory only and are never written to disk.
- **Debug logs** (`custom_components.sjwater: debug`) may include account details returned by the portal. Review logs before sharing them in bug reports or forums.

## Technical Details

| Property | Value |
|----------|-------|
| Domain | `sjwater` |
| Integration type | Hub |
| IoT class | Cloud Polling |
| Update interval | 1 hour |
| Dependencies | `recorder` |
| API endpoint | `https://www.sjwaterhub.com/api/WebApi/RequestBroker` |
| Authentication | Session token + anti-forgery token |
| Quality scale | Bronze |

### Files

| File | Purpose |
|------|---------|
| `__init__.py` | Entry point — sets up coordinator, initializes state, forwards to sensor platform |
| `api.py` | API client — login, token management, data fetching, response parsing |
| `coordinator.py` | DataUpdateCoordinator — polling, state persistence, statistics import |
| `sensor.py` | Sensor entities — Water Meter Total and Today's Water Usage |
| `config_flow.py` | UI config flow — username/password form, credential validation |
| `const.py` | Domain and config key constants |
| `manifest.json` | HA manifest — domain, version, dependencies |
| `quality_scale.yaml` | Integration Quality Scale rules and status |
| `brand/icon.png` | Integration icon (16KB) |
| `brand/logo.png` | Integration logo (39KB) |
| `translations/en.json` | English translation strings for config flow |
| `hacs.json` | HACS manifest for custom repository distribution |

## Troubleshooting

### "Failed to connect" error

- Verify your SJ Water Hub credentials at [sjwaterhub.com](https://www.sjwaterhub.com)
- Check that the portal URL hasn't changed (the integration scrapes `sjwaterhub.com`)
- Enable debug logging:
   ```yaml
   logger:
     logs:
       custom_components.sjwater: debug
   ```

### Sensor shows 0 or unavailable

- The API may have changed. Enable debug logging and check for parse errors
- The integration requires the `recorder` integration to be enabled for statistics import
- Check that your HA timezone matches the water company's timezone (the integration treats API timestamps as local time)

### Statistics / Energy Dashboard not populating

- Statistics are imported into the external statistic `sjwater:<hash>_water_usage` on each successful data fetch; look for it under **Developer Tools → Statistics**
- Historical data is not backfilled beyond what the API returns
- Select the `sjwater:` statistic (**SJ Water Hub Water Usage**) in the Energy Dashboard. The Water Meter Total sensor books usage at the time it was *fetched*, which can be up to a day after it was consumed
- Today's Water Usage does not import separate statistics (daily-resetting sums are incompatible with the Energy Dashboard's change computation)

## Removal

When you remove the integration via the UI, `async_remove_entry` cleans up the persisted state file from HA's storage. The imported `sjwater:<hash>_water_usage` statistic is kept; delete it under **Developer Tools → Statistics** if you no longer want the history.

## License

This integration is for personal use. The SJ Water Hub API is not publicly documented — reverse-engineered from the web portal.
