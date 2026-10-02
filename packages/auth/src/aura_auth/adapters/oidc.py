"""Generic OpenID Connect client for enterprise SSO (Okta, Entra ID, Google Workspace,
Keycloak, ...): authorization code + PKCE, identity from the userinfo endpoint.

Identity comes from the userinfo endpoint, called with an access token obtained
directly from the token endpoint over TLS by this confidential client. That channel
authenticates the IdP, so no ID-token signature check is needed for this flow.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from aura_auth.application.ports import OAuthUserInfo
from aura_shared.errors import DependencyUnavailable, Unauthenticated, ValidationFailed

REQUIRED = ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")


def check_url(url: str, *, allow_http_localhost: bool) -> str:
    p = urlparse(url)
    local = p.hostname in ("localhost", "127.0.0.1")
    if p.scheme == "https" and p.hostname:
        return url
    if allow_http_localhost and p.scheme == "http" and local:
        return url
    raise ValidationFailed(f"IdP endpoints must use https: {url!r}")


async def discover(
    http: httpx.AsyncClient, issuer: str, *, allow_http_localhost: bool = False
) -> dict[str, str]:
    """Fetch endpoints from ``{issuer}/.well-known/openid-configuration``."""
    base = check_url(issuer.rstrip("/"), allow_http_localhost=allow_http_localhost)
    resp = await http.get(f"{base}/.well-known/openid-configuration", timeout=10)
    if resp.status_code != 200:
        raise DependencyUnavailable("could not fetch the IdP's OpenID configuration")
    data: dict[str, Any] = resp.json()
    if str(data.get("issuer", "")).rstrip("/") != base:
        raise ValidationFailed("issuer in the discovery document does not match")
    out = {"issuer": base}
    for k in REQUIRED:
        if not data.get(k):
            raise ValidationFailed(f"IdP configuration is missing {k}")
        out[k] = check_url(str(data[k]), allow_http_localhost=allow_http_localhost)
    return out


class OidcClient:
    def __init__(self, config: dict[str, str], client_secret: str, http: httpx.AsyncClient) -> None:
        self._c, self._secret, self._http = config, client_secret, http

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str, login_hint: str | None
    ) -> str:
        query = {
            "client_id": self._c["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            query["login_hint"] = login_hint
        return f"{self._c['authorization_endpoint']}?{urlencode(query)}"

    async def exchange(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthUserInfo:
        token = await self._http.post(
            self._c["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._c["client_id"],
                "client_secret": self._secret,
                "code_verifier": code_verifier,
            },
            timeout=10,
        )
        if token.status_code != 200 or not token.json().get("access_token"):
            raise Unauthenticated("single sign-on failed at the identity provider")
        info = await self._http.get(
            self._c["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            timeout=10,
        )
        if info.status_code != 200:
            raise Unauthenticated("single sign-on failed at the identity provider")
        data = info.json()
        if not data.get("sub") or not data.get("email"):
            raise Unauthenticated("the identity provider returned no email")
        return OAuthUserInfo(
            subject=str(data["sub"]),
            email=str(data["email"]),
            # Enterprise IdPs often omit this claim; the domain check is the real gate.
            email_verified=data.get("email_verified", True) is not False,
            name=data.get("name"),
        )
