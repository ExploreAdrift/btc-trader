from __future__ import annotations

import base64
import time
from datetime import date
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from btc_trader.kalshi.auth import get_key_id, get_private_key_pem

_BASE_URL      = "https://api.elections.kalshi.com/trade-api/v2"
_DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
_API_PREFIX    = "/trade-api/v2"   # prepended to path when building the signed message


def _sign(private_key_pem: str, timestamp_ms: int, method: str, path: str) -> str:
    """Return base64-encoded RSA-PSS-SHA256 signature of '{timestamp}{METHOD}{full_path}'.

    Kalshi requires:
      - Full path including /trade-api/v2 prefix (not just /portfolio/balance)
      - RSA-PSS padding with MGF1(SHA256) and salt_length=DIGEST_LENGTH
      - Path stripped of query parameters before signing
    """
    # Strip query params from path before signing
    sign_path = path.split("?")[0]
    # Prepend /trade-api/v2 if not already present
    if not sign_path.startswith(_API_PREFIX):
        sign_path = _API_PREFIX + sign_path

    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    message = f"{timestamp_ms}{method.upper()}{sign_path}".encode()
    signature = private_key.sign(
        message,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class KalshiClient:
    def __init__(self, demo: bool = False, timeout: int = 15) -> None:
        self._base = _DEMO_BASE_URL if demo else _BASE_URL
        self._timeout = timeout
        self._key_id = get_key_id()
        self._private_key_pem = get_private_key_pem()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = int(time.time() * 1000)
        signature = _sign(self._private_key_pem, timestamp_ms, method.upper(), path)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._session.get(
            f"{self._base}{path}",
            params=params,
            headers=self._auth_headers("GET", path),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._session.post(
            f"{self._base}{path}",
            json=body,
            headers=self._auth_headers("POST", path),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
        resp = self._session.delete(
            f"{self._base}{path}",
            headers=self._auth_headers("DELETE", path),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_markets(
        self,
        series_ticker: str | None = None,
        status: str | None = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = self._get("/markets", params=params)
        return data.get("markets", [])

    def get_market(self, ticker: str) -> dict[str, Any]:
        data = self._get(f"/markets/{ticker}")
        return data.get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook", {})

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def create_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,        # "buy" or "sell"
        count: int,
        order_type: str = "limit",
        yes_price: int | None = None,
        no_price: int | None = None,
        expiration_ts: int | None = None,
    ) -> dict[str, Any]:
        """Place a limit or market order.

        Prices are in cents (1–99). For a limit buy-yes, supply yes_price.
        For a limit buy-no, supply no_price.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if expiration_ts is not None:
            body["expiration_ts"] = expiration_ts
        data = self._post("/portfolio/orders", body)
        return data.get("order", {})

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        data = self._delete(f"/portfolio/orders/{order_id}")
        return data.get("order", {})

    def get_orders(self, ticker: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = self._get("/portfolio/orders", params=params)
        return data.get("orders", [])

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self, ticker: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/positions", params=params)
        return data.get("market_positions", [])

    def get_balance(self) -> dict[str, Any]:
        return self._get("/portfolio/balance")
