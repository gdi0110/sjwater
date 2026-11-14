from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sjwater import (
    async_setup_entry,
    async_unload_entry,
    async_remove_entry,
    PLATFORMS,
)
from custom_components.sjwater.const import DOMAIN


class TestAsyncSetupEntry:
    async def test_full_setup_flow(self):
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = MagicMock()
        entry.data = {"username": "test@example.com", "password": "password123"}
        entry.entry_id = "test_entry_1"
        entry.runtime_data = None

        with patch(
            "custom_components.sjwater.async_get_clientsession",
        ) as mock_session:
            with patch(
                "custom_components.sjwater.SJWaterHubApiClient",
            ) as mock_client_cls:
                mock_client = mock_client_cls.return_value
                with patch(
                    "custom_components.sjwater.SJWaterHubCoordinator",
                ) as mock_coord_cls:
                    mock_coord = mock_coord_cls.return_value
                    mock_coord.async_initialize = AsyncMock()
                    mock_coord.async_config_entry_first_refresh = AsyncMock()

                    result = await async_setup_entry(hass, entry)

        assert result is True
        mock_client_cls.assert_called_once_with(
            username="test@example.com",
            password="password123",
            session=mock_session.return_value,
        )
        mock_coord_cls.assert_called_once_with(hass, entry, mock_client)
        mock_coord.async_initialize.assert_called_once()
        assert entry.runtime_data is mock_coord
        hass.config_entries.async_forward_entry_setups.assert_called_once_with(entry, PLATFORMS)
        mock_coord.async_config_entry_first_refresh.assert_called_once()

    async def test_setup_fails_on_initialize_error(self):
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = MagicMock()
        entry.data = {"username": "test@example.com", "password": "password123"}
        entry.entry_id = "test_entry_1"
        entry.runtime_data = None

        with patch(
            "custom_components.sjwater.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.SJWaterHubApiClient",
        ), patch(
            "custom_components.sjwater.SJWaterHubCoordinator",
        ) as mock_coord_cls:
            mock_coord = mock_coord_cls.return_value
            mock_coord.async_initialize = AsyncMock(side_effect=Exception("Init failed"))
            mock_coord.async_config_entry_first_refresh = AsyncMock()

            with pytest.raises(Exception, match="Init failed"):
                await async_setup_entry(hass, entry)

    async def test_setup_fails_on_forward_error(self):
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock(
            side_effect=Exception("Forward failed")
        )
        entry = MagicMock()
        entry.data = {"username": "test@example.com", "password": "password123"}
        entry.entry_id = "test_entry_1"
        entry.runtime_data = None

        with patch(
            "custom_components.sjwater.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.SJWaterHubApiClient",
        ), patch(
            "custom_components.sjwater.SJWaterHubCoordinator",
        ) as mock_coord_cls:
            mock_coord = mock_coord_cls.return_value
            mock_coord.async_initialize = AsyncMock()
            mock_coord.async_config_entry_first_refresh = AsyncMock()

            with pytest.raises(Exception, match="Forward failed"):
                await async_setup_entry(hass, entry)

    async def test_setup_fails_on_first_refresh_error(self):
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        entry = MagicMock()
        entry.data = {"username": "test@example.com", "password": "password123"}
        entry.entry_id = "test_entry_1"
        entry.runtime_data = None

        with patch(
            "custom_components.sjwater.async_get_clientsession",
        ), patch(
            "custom_components.sjwater.SJWaterHubApiClient",
        ), patch(
            "custom_components.sjwater.SJWaterHubCoordinator",
        ) as mock_coord_cls:
            mock_coord = mock_coord_cls.return_value
            mock_coord.async_initialize = AsyncMock()
            mock_coord.async_config_entry_first_refresh = AsyncMock(
                side_effect=Exception("Refresh failed")
            )

            with pytest.raises(Exception, match="Refresh failed"):
                await async_setup_entry(hass, entry)

        # Platforms must not be set up when the first refresh fails.
        hass.config_entries.async_forward_entry_setups.assert_not_called()


class TestAsyncUnloadEntry:
    async def test_unloads_sensor_platform(self):
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = MagicMock()

        result = await async_unload_entry(hass, entry)

        assert result is True
        hass.config_entries.async_unload_platforms.assert_called_once_with(entry, PLATFORMS)

    async def test_unload_returns_false_on_failure(self):
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
        entry = MagicMock()

        result = await async_unload_entry(hass, entry)

        assert result is False


class TestAsyncRemoveEntry:
    async def test_removes_store_data(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry_1"

        with patch(
            "homeassistant.helpers.storage.Store",
        ) as mock_store_cls:
            mock_store = mock_store_cls.return_value
            mock_store.async_remove = AsyncMock()

            await async_remove_entry(hass, entry)

        mock_store_cls.assert_called_once()
        mock_store.async_remove.assert_called_once()

    async def test_remove_fails_on_store_error(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry_1"

        with patch(
            "homeassistant.helpers.storage.Store",
        ) as mock_store_cls:
            mock_store = mock_store_cls.return_value
            mock_store.async_remove = AsyncMock(
                side_effect=Exception("Remove failed")
            )

            with pytest.raises(Exception, match="Remove failed"):
                await async_remove_entry(hass, entry)
