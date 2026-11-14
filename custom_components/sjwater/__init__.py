import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SJWaterHubApiClient
from .coordinator import SJWaterHubCoordinator
from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SJ Water Hub from a config entry."""

    session = async_get_clientsession(hass)
    client = SJWaterHubApiClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session
    )

    coordinator = SJWaterHubCoordinator(hass, entry, client)

    # 1. Restore persistent state (running sum from disk/recorder) BEFORE
    #    the first data fetch so _current_sum is pre-populated correctly.
    await coordinator.async_initialize()
    # 2. Store coordinator reference so sensors can access it.
    entry.runtime_data = coordinator

    # 3. Trigger the first data fetch to populate the entity state and
    #    backfill historical statistics into the Energy Dashboard. This runs
    #    before the platforms are forwarded: if it fails, ConfigEntryNotReady
    #    is raised with nothing set up yet, so Home Assistant can retry cleanly.
    await coordinator.async_config_entry_first_refresh()

    # 4. Forward the config entry to the sensor platform to create entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wipe per-entry persisted state when the integration is removed."""
    from homeassistant.helpers.storage import Store
    import json
    store = Store(hass, version=1, key=f"{DOMAIN}_state_{entry.entry_id}", encoder=json.JSONEncoder)
    await store.async_remove()
