from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import data_entry_flow

from custom_components.sjwater.config_flow import ConfigFlow, validate_input
from custom_components.sjwater.const import DOMAIN
from custom_components.sjwater.api import InvalidAuth, CannotConnect




def _make_flow():
    flow = ConfigFlow()
    flow.context = {}
    flow.hass = MagicMock()
    flow.hass.data = {}
    flow.hass.config_entries = MagicMock()
    flow.hass.config_entries.flow = MagicMock()
    flow.hass.config_entries.flow.async_progress_by_handler = MagicMock(return_value=[])
    flow.hass.config_entries.async_entries = MagicMock(return_value=[])
    flow.hass.config_entries.async_entry_for_domain_unique_id = MagicMock(return_value=None)
    return flow


class TestAsyncStepUser:
    async def test_form_displayed_on_first_call(self):
        flow = _make_flow()

        result = await flow.async_step_user(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert "username" in result["data_schema"].schema
        assert "password" in result["data_schema"].schema
        assert result["errors"] == {}

    async def test_valid_credentials_creates_entry(self):
        flow = _make_flow()

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            new=AsyncMock(return_value={"title": "SJ Water Hub"}),
        ):
            result = await flow.async_step_user(
                user_input={"username": "test@example.com", "password": "password123"}
            )

        assert result["type"] == "create_entry"
        assert result["title"] == "SJ Water Hub"
        assert result["data"] == {"username": "test@example.com", "password": "password123"}

    async def test_invalid_auth_shows_error(self):
        flow = _make_flow()

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            side_effect=InvalidAuth,
        ):
            result = await flow.async_step_user(
                user_input={"username": "bad@user.com", "password": "wrong"}
            )

        assert result["type"] == "form"
        assert result["errors"]["base"] == "invalid_auth"

    async def test_cannot_connect_shows_error(self):
        flow = _make_flow()

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            side_effect=CannotConnect,
        ):
            result = await flow.async_step_user(
                user_input={"username": "test@example.com", "password": "password123"}
            )

        assert result["type"] == "form"
        assert result["errors"]["base"] == "cannot_connect"

    async def test_unknown_exception_shows_error(self):
        flow = _make_flow()

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            side_effect=Exception("Unexpected error"),
        ):
            result = await flow.async_step_user(
                user_input={"username": "test@example.com", "password": "password123"}
            )

        assert result["type"] == "form"
        assert result["errors"]["base"] == "unknown"

    async def test_duplicate_entry_aborts(self):
        flow = _make_flow()

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            new=AsyncMock(return_value={"title": "SJ Water Hub"}),
        ):
            result = await flow.async_step_user(
                user_input={"username": "test@example.com", "password": "password123"}
            )
            assert result["type"] == "create_entry"

        flow_dup = _make_flow()
        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            new=AsyncMock(return_value={"title": "SJ Water Hub"}),
        ), patch.object(
            flow_dup.__class__,
            "_abort_if_unique_id_configured",
            side_effect=data_entry_flow.AbortFlow("already_configured"),
        ):
            with pytest.raises(data_entry_flow.AbortFlow):
                await flow_dup.async_step_user(
                    user_input={"username": "test@example.com", "password": "password123"}
                )

    async def test_unique_id_derivation(self):
        flow = _make_flow()

        username = "test@example.com"
        expected_id = hashlib.sha256(username.encode()).hexdigest()[:8]

        with patch(
            "custom_components.sjwater.config_flow.validate_input",
            new=AsyncMock(return_value={"title": "SJ Water Hub"}),
        ):
            result = await flow.async_step_user(
                user_input={"username": username, "password": "password123"}
            )

        assert result["type"] == "create_entry"
        assert flow.unique_id == expected_id


class TestValidateInput:
    async def test_success(self, hass):
        with patch(
            "custom_components.sjwater.config_flow.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.config_flow.SJWaterHubApiClient",
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.async_verify_credentials = AsyncMock(return_value=True)

            result = await validate_input(hass, {"username": "test@example.com", "password": "password123"})

            assert result == {"title": "SJ Water Hub"}

    async def test_raises_invalid_auth(self, hass):
        with patch(
            "custom_components.sjwater.config_flow.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.config_flow.SJWaterHubApiClient",
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.async_verify_credentials = AsyncMock(
                side_effect=InvalidAuth
            )

            with pytest.raises(InvalidAuth):
                await validate_input(hass, {"username": "bad", "password": "wrong"})

    async def test_raises_cannot_connect(self, hass):
        with patch(
            "custom_components.sjwater.config_flow.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.config_flow.SJWaterHubApiClient",
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.async_verify_credentials = AsyncMock(
                side_effect=CannotConnect
            )

            with pytest.raises(CannotConnect):
                await validate_input(hass, {"username": "test", "password": "test"})
            mock_client.async_verify_credentials.assert_called_once()


class TestOptionsFlow:
    def _make_options_flow(self, options=None):
        from custom_components.sjwater.config_flow import OptionsFlow

        flow = OptionsFlow()
        entry = MagicMock()
        entry.options = options or {}
        with patch.object(OptionsFlow, "config_entry", entry, create=True):
            yield flow

    async def test_form_suggests_current_price(self):
        for flow in self._make_options_flow({"price_per_gallon": 0.00754}):
            result = await flow.async_step_init(user_input=None)
            assert result["type"] == "form"
            assert result["step_id"] == "init"
            (key,) = result["data_schema"].schema
            assert key == "price_per_gallon"
            assert key.description["suggested_value"] == 0.00754

    async def test_saves_price(self):
        for flow in self._make_options_flow():
            result = await flow.async_step_init(user_input={"price_per_gallon": 0.00754})
            assert result["type"] == "create_entry"
            assert result["data"] == {"price_per_gallon": 0.00754}

    async def test_rejects_negative_price(self):
        for flow in self._make_options_flow():
            result = await flow.async_step_init(user_input=None)
            with pytest.raises(vol.Invalid):
                result["data_schema"]({"price_per_gallon": -1})

    def test_config_flow_offers_options(self):
        from custom_components.sjwater.config_flow import OptionsFlow

        assert isinstance(ConfigFlow.async_get_options_flow(MagicMock()), OptionsFlow)
