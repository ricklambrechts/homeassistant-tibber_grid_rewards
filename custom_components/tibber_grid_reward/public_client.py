import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)

PUBLIC_API_URL = "https://api.tibber.com/v1-beta/gql"


class TibberPublicException(Exception):
    """Base exception for the Tibber public API client."""


class TibberPublicAuthError(TibberPublicException):
    """Exception for authentication errors."""


def _parse_graphql_viewer(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return viewer data and error messages, raising for authentication failures."""
    messages = []
    for error in data.get("errors") or []:
        message = error.get("message", "Unknown GraphQL error")
        code = (error.get("extensions") or {}).get("code")
        _LOGGER.error("Public API GraphQL error: %s (code: %s)", message, code)
        if code in ("UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"):
            raise TibberPublicAuthError(message)
        messages.append(message)
    return (data.get("data") or {}).get("viewer") or {}, messages


def _parse_price_info(
    home: dict[str, Any], errors: list[str]
) -> tuple[bool, dict[str, Any] | None]:
    """Return whether to cache this result, and the prices to cache."""
    subscription = home.get("currentSubscription", {})
    price_info = (subscription or {}).get("priceInfo")
    if isinstance(price_info, dict):
        return True, price_info
    # Cache absence only when the field is explicitly null and error-free.
    return subscription is None and not errors, None


class TibberPublicAPI:
    """A client for the public Tibber API."""

    def __init__(self, token: str, client: httpx.AsyncClient):
        """Initialize the client."""
        self._token = token
        self._client = client
        self.headers = {
            "Authorization": f"Bearer {self._token}",
        }
        self._price_cache = {}
        self._price_cache_time = {}

    async def get_homes(self) -> list[dict[str, Any]]:
        """Fetch Tibber homes."""
        _LOGGER.debug("Fetching Tibber homes from public API.")
        query = "{ viewer { homes { id appNickname address { address1 } } } }"
        payload = {"query": query}
        try:
            response = await self._client.post(
                PUBLIC_API_URL, headers=self.headers, json=payload
            )
            response.raise_for_status()
            viewer, errors = _parse_graphql_viewer(response.json())
            homes = [
                home
                for home in viewer.get("homes") or []
                if isinstance(home, dict) and home.get("id")
            ]
            if errors and not homes:
                raise TibberPublicException("; ".join(errors))
            _LOGGER.debug("Successfully fetched Tibber homes from public API.")
            for home in homes:
                home["title"] = home.get("appNickname") or (
                    home.get("address") or {}
                ).get("address1", home["id"])
            return homes
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                _LOGGER.error("Authentication failed with public API.")
                raise TibberPublicAuthError from e
            _LOGGER.error("Could not fetch homes from public API: %s", e)
            raise TibberPublicException from e
        except TibberPublicException:
            raise
        except Exception as e:
            _LOGGER.error("An unexpected error occurred while fetching homes: %s", e)
            raise TibberPublicException from e

    async def get_all_homes_price_info(self) -> dict[str, dict[str, Any]]:
        """Fetch price info for all homes on the account in a single request."""
        _LOGGER.debug("Fetching price info for all homes from public API.")
        now = datetime.now(UTC)
        query = """
        query {
          viewer {
            homes {
              id
              currentSubscription {
                priceInfo {
                  today {
                    total
                    energy
                    tax
                    startsAt
                    currency
                  }
                  tomorrow {
                    total
                    energy
                    tax
                    startsAt
                    currency
                  }
                }
              }
            }
          }
        }
        """
        payload = {"query": query}
        try:
            response = await self._client.post(
                PUBLIC_API_URL, headers=self.headers, json=payload
            )
            response.raise_for_status()
            viewer, errors = _parse_graphql_viewer(response.json())
            homes = viewer.get("homes") or []
            results = {}
            for home in homes:
                if not isinstance(home, dict):
                    continue
                home_id = home.get("id")
                if not home_id:
                    continue
                cacheable, price_info = _parse_price_info(home, errors)
                if not cacheable:
                    continue
                self._price_cache[home_id] = price_info
                self._price_cache_time[home_id] = now
                if price_info:
                    results[home_id] = price_info
            _LOGGER.debug("Fetched available homes price info from public API.")
            return results
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                _LOGGER.error("Authentication failed with public API.")
                raise TibberPublicAuthError from e
            _LOGGER.error("Could not fetch all price info from public API: %s", e)
            return {}
        except TibberPublicAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "An unexpected error occurred while fetching all price info: %s", e
            )
            return {}

    async def get_price_info(self, home_id: str) -> dict[str, Any] | None:
        """Fetch price info for a specific home, utilizing multi-home batch cache."""
        now = datetime.now(UTC)
        cache_time = self._price_cache_time.get(home_id)
        if cache_time and now - cache_time < timedelta(hours=6):
            _LOGGER.debug("Returning cached price info for home %s.", home_id)
            return self._price_cache.get(home_id)

        # Batch fetch all homes' prices first to warm cache for all homes
        await self.get_all_homes_price_info()
        cache_time = self._price_cache_time.get(home_id)
        if cache_time and now - cache_time < timedelta(hours=6):
            return self._price_cache.get(home_id)

        _LOGGER.debug(
            "Fetching fallback price info for home %s from public API.", home_id
        )
        query = """
        query($homeId: ID!) {
          viewer {
            home(id: $homeId) {
              currentSubscription {
                priceInfo {
                  today {
                    total
                    energy
                    tax
                    startsAt
                    currency
                  }
                  tomorrow {
                    total
                    energy
                    tax
                    startsAt
                    currency
                  }
                }
              }
            }
          }
        }
        """
        payload = {"query": query, "variables": {"homeId": home_id}}
        try:
            response = await self._client.post(
                PUBLIC_API_URL, headers=self.headers, json=payload
            )
            response.raise_for_status()
            viewer, errors = _parse_graphql_viewer(response.json())
            cacheable, price_info = _parse_price_info(viewer.get("home") or {}, errors)
            if not cacheable:
                return None
            self._price_cache[home_id] = price_info
            self._price_cache_time[home_id] = now
            _LOGGER.debug("Fetched available price info from public API.")
            return price_info
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                _LOGGER.error("Authentication failed with public API.")
                raise TibberPublicAuthError from e
            _LOGGER.error("Could not fetch price info from public API: %s", e)
            return None
        except TibberPublicAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "An unexpected error occurred while fetching price info: %s", e
            )
            return None
