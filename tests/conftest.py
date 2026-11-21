from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.sjwater.api import SJWaterHubApiClient
from custom_components.sjwater.coordinator import (
    SJWaterHubCoordinator,
    SCAN_INTERVAL,
)
from custom_components.sjwater.const import DOMAIN

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def load_json_fixture(name: str) -> dict:
    return json.loads(load_fixture(name))


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.post = MagicMock()
    session.get = MagicMock()
    return session


@pytest.fixture
def api_client(mock_session):
    return SJWaterHubApiClient("test@example.com", "password123", mock_session)


@pytest.fixture
def authenticated_client(api_client, mock_session):
    api_client._token = "test_token_123"
    api_client._account_guid = "1234567890"
    return api_client


def _make_async_resp(status=200, data=None, text=""):
    resp = AsyncMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    resp.raise_for_status = MagicMock()
    if data is not None:
        resp.json = AsyncMock(return_value=data)
    if text:
        resp.text = AsyncMock(return_value=text)
    return resp


def _mock_post_response(mock_session, status=200, data=None):
    resp = _make_async_resp(status=status, data=data)
    mock_session.post.return_value = resp
    return resp


def _mock_get_response(mock_session, text="", status=200):
    resp = _make_async_resp(status=status, text=text)
    mock_session.get.return_value = resp
    return resp


@pytest.fixture
def mock_post_factory(mock_session):
    def factory(data: dict, status: int = 200):
        return _mock_post_response(mock_session, status=status, data=data)
    return factory


@pytest.fixture
def mock_get_factory(mock_session):
    def factory(text: str = "", status: int = 200):
        return _mock_get_response(mock_session, text=text, status=status)
    return factory


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.async_create_task = AsyncMock()
    return hass


@pytest.fixture
def config_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_1"
    entry.data = {"username": "test@example.com", "password": "password123"}
    entry.title = "SJ Water Hub"
    entry.runtime_data = None
    entry.options = {}
    return entry


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    store.async_remove = AsyncMock()
    return store


@pytest.fixture
def coordinator(hass, config_entry, api_client, mock_store):
    with patch(
        "custom_components.sjwater.coordinator.Store",
        return_value=mock_store,
    ):
        coord = SJWaterHubCoordinator(hass, config_entry, api_client)
        return coord

