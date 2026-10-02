"""Google OpenID Connect provider (authorization code + PKCE)."""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from aura_auth.application.ports import OAuthUserInfo
from aura_shared.errors import Unauthenticated

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuth:
    name = "google"

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._id, self._secret, self._http = client_id, client_secret, http

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        query = {
            "client_id": self._id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    async def exchange(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthUserInfo:
        token = await self._http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self._id,
                "client_secret": self._secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=10,
        )
        if token.status_code != 200:
            raise Unauthenticated("Google sign-in failed")
        access_token = token.json().get("access_token")
        info = await self._http.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10
        )
        if info.status_code != 200:
            raise Unauthenticated("Google sign-in failed")
        data = info.json()
        if not data.get("sub") or not data.get("email"):
            raise Unauthenticated("Google account has no email")
        return OAuthUserInfo(
            subject=str(data["sub"]),
            email=str(data["email"]),
            email_verified=bool(data.get("email_verified")),
            name=data.get("name"),
        )
