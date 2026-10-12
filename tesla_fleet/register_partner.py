"""One-time partner registration: ties the developer app to the domain hosting its public key."""

from __future__ import annotations

import sys

import httpx

from .config import AUTH_BASE, PARTNER_SCOPES, load_settings


def main() -> None:
    settings = load_settings()
    if not (settings.client_secret and settings.domain):
        sys.exit("TESLA_CLIENT_SECRET and TESLA_DOMAIN must be set in the private .env file.")
    with httpx.Client(timeout=30) as http:
        token = http.post(
            f"{AUTH_BASE}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "scope": PARTNER_SCOPES,
                "audience": settings.api_base,
            },
        )
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

        register = http.post(
            f"{settings.api_base}/api/1/partner_accounts", headers=headers, json={"domain": settings.domain}
        )
        if register.status_code >= 400:
            sys.exit(f"Registration failed ({register.status_code}): {register.text[:300]}")
        print(f"Registered {settings.domain} with Tesla Fleet API ({settings.region}).")

        check = http.get(
            f"{settings.api_base}/api/1/partner_accounts/public_key",
            headers=headers,
            params={"domain": settings.domain},
        )
        if check.status_code == 200:
            print("Tesla can read the hosted public key.")
        else:
            print(f"Warning: public key check returned {check.status_code}. Confirm the key URL loads in a browser.")


if __name__ == "__main__":
    main()
