import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SJWaterHubApiClient, InvalidAuth, CannotConnect
from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_PRICE_PER_GALLON

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    """Validate the user input allows us to connect."""
    session = async_get_clientsession(hass)
    client = SJWaterHubApiClient(data[CONF_USERNAME], data[CONF_PASSWORD], session)

    await client.async_verify_credentials()

    return {"title": "SJ Water Hub"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SJ Water Hub."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow (price per gallon for the cost statistic)."""
        return OptionsFlow()

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            import hashlib
            acct_id = hashlib.sha256(user_input[CONF_USERNAME].encode()).hexdigest()[:8]
            await self.async_set_unique_id(acct_id)
            self._abort_if_unique_id_configured()

            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class OptionsFlow(config_entries.OptionsFlowWithReload):
    """Set the price per gallon used for the water cost statistic."""

    async def async_step_init(self, user_input=None):
        """Show or save the price. Leave it empty to turn cost tracking off."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PRICE_PER_GALLON,
                    description={
                        "suggested_value": self.config_entry.options.get(
                            CONF_PRICE_PER_GALLON
                        )
                    },
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
