"""Thin wrapper around the Interactive Brokers Client Portal REST API.

The IBKR Client Portal Gateway must be running locally and the user must be
authenticated via the gateway's browser login flow before this client can
fetch market data. The gateway listens on https://localhost:5000/v1/api by
default and serves a self-signed certificate, hence SSL verification is off
by default.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger(__name__)


class IBKRError(RuntimeError):
    """Raised for any failure talking to the Client Portal gateway."""


class IBKRClient:
    def __init__(
        self,
        base_url: str = "https://localhost:5000/v1/api",
        verify_ssl: bool = False,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update(
            {"User-Agent": "spx-expected-move/1.0", "Accept": "application/json"}
        )

    def _req(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        LOG.debug("%s %s params=%s body=%s", method, url, params, json_body)
        try:
            r = self.session.request(
                method, url, params=params, json=json_body, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise IBKRError(f"network error contacting {url}: {e}") from e
        if r.status_code >= 400:
            raise IBKRError(
                f"{method} {url} -> HTTP {r.status_code}: {r.text[:300]}"
            )
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise IBKRError(
                f"non-JSON response from {url}: {r.text[:200]}"
            ) from e

    # Session management — the gateway needs a periodic tickle and the
    # brokerage session needs to be initialized once before market data flows.
    def tickle(self) -> Any:
        return self._req("POST", "/tickle")

    def auth_status(self) -> dict:
        return self._req("POST", "/iserver/auth/status")

    def init_brokerage_session(self) -> Any:
        return self._req("GET", "/iserver/accounts")

    # Symbol resolution
    def search_symbol(self, symbol: str, sec_type: str = "IND") -> list:
        return self._req(
            "POST",
            "/iserver/secdef/search",
            json_body={"symbol": symbol, "name": False, "secType": sec_type},
        )

    # Option chain navigation
    def strikes(
        self,
        conid: int,
        month: str,
        exchange: str = "SMART",
        sec_type: str = "OPT",
    ) -> dict:
        return self._req(
            "GET",
            "/iserver/secdef/strikes",
            params={
                "conid": conid,
                "sectype": sec_type,
                "month": month,
                "exchange": exchange,
            },
        )

    def contract_info(
        self,
        conid: int,
        sectype: str = "OPT",
        month: Optional[str] = None,
        strike: Optional[float] = None,
        right: Optional[str] = None,
        exchange: str = "SMART",
    ) -> Any:
        params: dict[str, Any] = {"conid": conid, "sectype": sectype, "exchange": exchange}
        if month:
            params["month"] = month
        if strike is not None:
            params["strike"] = strike
        if right:
            params["right"] = right
        return self._req("GET", "/iserver/secdef/info", params=params)

    # Market data
    def snapshot(
        self, conids: list[int], fields: list[str], poll_count: int = 4
    ) -> list[dict]:
        """Fetch a market data snapshot.

        IBKR's snapshot endpoint streams data lazily — the first call may
        return only the conid with no field values. We poll a few times until
        every row has at least one of the requested fields populated.
        """
        conids_str = ",".join(map(str, conids))
        fields_str = ",".join(fields)
        params = {"conids": conids_str, "fields": fields_str}
        last: list[dict] = []
        for attempt in range(poll_count):
            result = self._req("GET", "/iserver/marketdata/snapshot", params=params)
            if not isinstance(result, list):
                result = []
            last = result
            if result and all(
                any(f in row for f in fields) for row in result
            ):
                return result
        return last
