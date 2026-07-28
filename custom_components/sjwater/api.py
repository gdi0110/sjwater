import json
import logging
import re

import aiohttp
from dateutil.parser import parse as dt_parse

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# The portal silently rejects logins from non-browser clients (Success=false,
# ErrorCode=0), so all requests must present browser-like headers.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""


class CannotConnect(HomeAssistantError):
    """Error to indicate connection failure."""


class SJWaterHubApiClient:
    def __init__(self, username, password, session: aiohttp.ClientSession):
        self.username = username
        self.password = password
        self.session = session
        self.base_url = "https://www.sjwaterhub.com/api/WebApi/RequestBroker"
        self._token = None
        self._account_guid = None

    async def _post_request(self, actions: str, view_name: str, payload_additions: dict = None) -> dict:
        """Helper to send the double-encoded JSON the RequestBroker API expects."""

        # Build the inner JSON payload (mirrors the fields the portal's own
        # requestBroker() JS helper sends -- no extras like IsMobile)
        inner_payload = {
            "Actions": actions,
            "Token": self._token if self._token else "",
            "ViewName": view_name,
            "SitePrefix": ""
        }

        if payload_additions:
            inner_payload.update(payload_additions)

        # Wrap it in the outer Request dict
        outer_payload = {
            "Request": json.dumps(inner_payload)
        }

        # Match the browser's $.ajax call exactly: JSON body, browser headers
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.sjwaterhub.com",
            "Referer": "https://www.sjwaterhub.com/Login",
        }

        _LOGGER.debug("Sending RequestBroker Actions: %s", actions)

        async with self.session.post(self.base_url, data=json.dumps(outer_payload), headers=headers) as response:
            response.raise_for_status()
            response_json = await response.json()
            return response_json

    async def async_login(self) -> bool:
        """Authenticate and grab the session Token and Account Guidance."""

        # 1. Provide the necessary GET request to establish initially required cookies
        #    such as 'ApplicationGatewayAffinity' and extract the anti-forgery Token.
        login_url = "https://www.sjwaterhub.com/Login"
        get_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with self.session.get(login_url, headers=get_headers) as page_response:
            page_response.raise_for_status()
            html = await page_response.text()

            # 2. Extract Token using regex from the hidden input: id="Token"
            match = re.search(r'id="Token"[^>]*value="([^"]+)"', html)
            if not match:
                _LOGGER.error("Failed to extract anti-forgery Token from the login page! The layout may have changed.")
                return False

            self._token = match.group(1)
            _LOGGER.debug("Extracted initial session Token")

        # 2b. Register the token with the server, mirroring createExceptions()
        # in the portal's page-load JS -- login is rejected without this step.
        exc_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.sjwaterhub.com",
            "Referer": "https://www.sjwaterhub.com/Login",
        }
        async with self.session.post(
            "https://www.sjwaterhub.com/api/WebApi/CreateExceptionPermissions",
            data={"token": self._token, "sitePrefix": ""},
            headers=exc_headers,
        ) as exc_response:
            _LOGGER.debug("CreateExceptionPermissions status: %s", exc_response.status)

        # 3. Use the extracted token to login
        login_additions = {
            "UserName": self.username,
            "Password": self.password,
            "Language": "",
            "ExistingMFAToken": None,
            "RedirectUrl": "",
            "AdditionalParameters": {"TargetElementId": "Login_Password"}
        }

        data = await self._post_request("VXengage_Login", "Login", login_additions)

        # Log only non-sensitive status fields. The full response carries the
        # session Token and PII in Transaction.Attributes (account holder
        # name, service address, email, etc.), so it must never be dumped
        # to the log.
        _LOGGER.debug(
            "Login response: Success=%s, Authorized=%s, Valid=%s",
            data.get("VXengage_Login", {}).get("Success"),
            data.get("Authorized"),
            data.get("Valid"),
        )

        _LOGGER.debug("Login completed")

        # A successful login returns {'Authorized': True} and {'Valid': True}
        # The internal 'VXengage_Login' object contains the Success stat.
        login_result = data.get("VXengage_Login", {})

        if not login_result.get("Success"):
            _LOGGER.error("Login failed")
            return False

        # Update our session token for subsequent data requests!
        if data.get("Token"):
            self._token = data.get("Token")

        # Dynamically discover the account number from login response
        # attributes. Different account types may use different keys.
        tx_attributes = login_result.get("Transaction", {}).get("Attributes", {})

        for key, value in tx_attributes.items():
            if isinstance(value, str) and value.isdigit() and len(value) >= 6:
                self._account_guid = value
                _LOGGER.debug("Discovered account number from attribute key: %s", key)
                break

        return True

    async def async_verify_credentials(self) -> bool:
        """Verify credentials by attempting a login.

        Raises InvalidAuth on authentication failure or CannotConnect on
        network/connection errors. Returns True on success.
        """
        try:
            success = await self.async_login()
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Cannot reach SJ Water Hub: {err}") from err
        except Exception as err:
            raise CannotConnect(f"Unexpected error during login: {err}") from err

        if not success:
            raise InvalidAuth("Invalid username or password")
        return True

    async def async_get_data(self, start_date_query: str = "", end_date_query: str = "", account_number: str = None) -> dict:
        """Fetch data from the water company's RequestBroker API."""

        # Ensure we are logged in first
        if not self._token:
            await self.async_login()

        # Try to use configured account number, or fallback to auto-detected one
        account = account_number or self._account_guid
        if not account:
            raise CannotConnect("Account number could not be discovered from login response")

        # Using the exact payload expected by VXengage_GetHourlyGraph
        data_additions = {
            "AccountNumber": account,
            "Template": "HourlyWaterUsage",
            "StartDate": start_date_query,
            "EndDate": end_date_query,
            "GroupData": True
        }

        data = await self._post_request("VXengage_GetHourlyGraph", "UnderstandUsage", data_additions)

        if data.get("SessionExpired"):
            _LOGGER.debug("Session expired. Re-authenticating...")
            login_success = await self.async_login()
            if not login_success:
                raise Exception("Re-authentication failed")
            data = await self._post_request("VXengage_GetHourlyGraph", "UnderstandUsage", data_additions)

        # Extract the VXengage_GetHourlyGraph inner response
        graph_data = data.get("VXengage_GetHourlyGraph", {})

        if not graph_data or not graph_data.get("Success"):
            _LOGGER.error("Failed to fetch hourly graph data")
            raise Exception("API returned unsuccessful response for hourly graph.")

        # Update our token if it revolved
        if data.get("Token"):
            self._token = data.get("Token")

        # 3. Parse the returned JSON dict
        # The schema uses ComplexSlidingGraphDataSets, SlidingGraphDataSets, Labels, etc.
        # We need the most recent usage point (gallons) and the timestamp
        datasets = graph_data.get("ComplexSlidingGraphDataSets", [])
        labels = graph_data.get("ComplexLabels", [])

        parsed_gallons = None
        parsed_timestamp = None
        parsed_history = []

        if datasets and labels:
            try:
                gallons_data = datasets[0].get("data", [])

                # Get the local timezone from HA's configuration
                local_tz = dt_util.DEFAULT_TIME_ZONE

                for idx, val in enumerate(gallons_data):
                    if val != "":
                        try:
                            # Convert label string into a datetime object
                            label_val = labels[idx] if idx < len(labels) else ""
                            # If it's a dict from ComplexLabels, try to extract a 'Date' or similar key
                            if isinstance(label_val, dict):
                                label_str = label_val.get("Date", label_val.get("Label", str(label_val)))
                            elif isinstance(label_val, list) and len(label_val) > 0:
                                label_str = " ".join(str(x) for x in label_val)
                            else:
                                label_str = str(label_val)

                            try:
                                dt_naive = dt_parse(label_str)
                            except Exception as parse_e:
                                _LOGGER.debug("Failed to parse label '%s': %s", label_str, parse_e)
                                dt_naive = dt_util.now()

                            # The SJ Water company data is in local time, not UTC.
                            # If we inject local time as UTC, Home Assistant offsets it by the
                            # timezone offset, shifting readings to wrong hours.
                            # Make the naive datetime aware with the local timezone.
                            if dt_naive.tzinfo is None:
                                dt_aware = dt_naive.replace(tzinfo=local_tz)
                            else:
                                dt_aware = dt_naive

                            # Convert to UTC for Home Assistant's database
                            dt_utc = dt_util.as_utc(dt_aware)

                            parsed_history.append({
                                "start": dt_utc,
                                "state": max(0.0, float(val))
                            })

                        except Exception as dt_err:
                            _LOGGER.debug("Could not parse date %s: %s", labels[idx] if idx < len(labels) else 'UNKNOWN', dt_err)

                # 2. Grab the latest reading for the main sensor
                # Get the last valid data point (ignoring empty strings if any)
                for index in range(len(gallons_data) - 1, -1, -1):
                    val = gallons_data[index]
                    if val != "":
                        parsed_gallons = float(val)
                        # The label for this point
                        label_str = labels[index] if index < len(labels) else ""
                        _LOGGER.debug("Found latest graph data: %s at %s", parsed_gallons, label_str)
                        # Also set the timestamp from this reading's entry in parsed_history
                        # (find the matching entry by index/value)
                        for h in reversed(parsed_history):
                            if abs(h["state"] - parsed_gallons) < 0.001:
                                parsed_timestamp = h["start"]
                                break
                        break

            except Exception as e:
                _LOGGER.error("Error parsing graph data sets: %s", e)

        # If we couldn't parse any gallons, default to 0
        if parsed_gallons is None:
            parsed_gallons = 0.0
        else:
            parsed_gallons = max(0.0, parsed_gallons)

        # Use the actual timestamp from the API, or fall back to now()
        if parsed_timestamp is None:
            parsed_timestamp = dt_util.utcnow()

        return {
            "gallons": parsed_gallons,
            "timestamp": parsed_timestamp,
            "last_updated": graph_data.get("LastUpdated", ""),
            "history": parsed_history,
        }
