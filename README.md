<p align="center">
  <img src="brand/logo.png" alt="Logo" height="150" valign="middle">
</p>

Custom Home Assistant integration for **SJ Water Company** (sjwaterhub.com). Fetches hourly water usage data from the water company's web portal and exposes it as Home Assistant sensors with full Energy Dashboard support.

## Features

- **Hourly water usage** — Scrapes the SJ Water Hub portal API for per-hour consumption data (in gallons)
- **Two sensors per account:**
  - **Water Meter Total** (`sensor.sjwater_<id>_water_usage`) — Monotonically increasing total, suitable for the HA Energy Dashboard
  - **Today's Water Usage** (`sensor.sjwater_<id>_todays_water_usage`) — Daily reset counter showing current day's consumption
- **Energy Dashboard compatible** — Imports historical statistics so the water meter appears alongside gas/electric usage
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
3. Select the **Water Meter Total** sensor
4. The integration imports hourly statistics so historical data populates immediately

## How It Works

### Authentication

The integration authenticates against `sjwaterhub.com` using a two-step process:

1. **GET** the login page to extract an anti-forgery token from a hidden `<input id="Token">` field
2. **POST** credentials via the `VXengage_Login` action through the `RequestBroker` API

After authentication, the session token is stored and refreshed on each API call. If the session expires, the integration automatically re-authenticates.

### Data Fetching

The coordinator polls `VXengage_GetHourlyGraph` every **1 hour** (`SCAN_INTERVAL = timedelta(hours=1)`). Each fetch returns:

- Per-hour water consumption in gallons (delta values, not cumulative)
- Timestamps for each reading (local time, converted to UTC for HA)
- Last-updated timestamp

New readings are tracked via `_last_processed_start` to avoid duplicate imports. The running sum (`_current_sum`) is persisted to disk so it survives restarts.

### State Persistence

The coordinator maintains a running total across restarts using `Store` — Home Assistant's key-value storage, persisting `current_sum` and `last_processed_start`.

This prevents the "midnight reset" artifact where a restart could re-import the day's data from `sum=0` and clobber the accumulated total.

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

- Statistics are imported on each successful data fetch for new hourly readings only
- Historical data is not backfilled beyond what the API returns
- The Water Meter Total sensor uses monotonically increasing cumulative sums and is the recommended sensor for the Energy Dashboard
- Today's Water Usage does not import separate statistics (daily-resetting sums are incompatible with the Energy Dashboard's change computation)

## Removal

When you remove the integration via the UI, `async_remove_entry` cleans up the persisted state file from HA's storage. You should also manually delete any related recorder statistics if desired.

## License

This integration is for personal use. The SJ Water Hub API is not publicly documented — reverse-engineered from the web portal.
