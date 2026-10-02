"""Enterprise single sign-on (spec §115): verified domains, OIDC and SAML connections,
SP-initiated login, just-in-time provisioning and SSO enforcement.

Trust model:
- An organization must prove it controls a domain (DNS TXT) before SSO can sign in
  anyone on it. An IdP can only assert emails on its organization's verified domains,
  so a malicious IdP cannot sign in as ``ceo@other-company.com``.
- Because the domain is proven, the IdP is authoritative for those emails: an SSO
  sign-in links to an existing user with that email.
- When a connection is *enforced*, magic links and Google sign-in are refused for
  emails on its domains.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import select

from aura_auth.adapters import oidc, saml
from aura_auth.adapters.dns import TxtResolver
from aura_auth.application.dto import UserView
from aura_auth.application.identity import IdentityService
from aura_auth.application.members import MemberService
from aura_auth.domain.emails import normalize_email
from aura_auth.domain.rbac import Role
from aura_db import Database, audit
from aura_db.models import Membership, Organization, SsoConnection, VerifiedDomain
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import Conflict, Forbidden, NotFound, Unauthenticated, ValidationFailed
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily, build_key, ttl_for

GLOBAL = "_global"
TXT_PREFIX = "aura-verify="
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Shared mailbox providers: nobody can "own" these for SSO.
FREE_MAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "rediffmail.com",
        "zoho.com",
        "yandex.com",
        "gmx.com",
    }
)
ASSIGNABLE_DEFAULT_ROLES = frozenset({r.value for r in Role} - {Role.OWNER.value})


def _aad(account_id: str, connection_id: str) -> str:
    return f"{account_id}|sso|{connection_id}"


def normalize_domain(raw: str) -> str:
    domain = raw.strip().lower().rstrip(".")
    if not _DOMAIN.match(domain):
        raise ValidationFailed("enter a domain like example.com")
    if domain in FREE_MAIL:
        raise ValidationFailed("shared email providers cannot be verified")
    return domain


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[1]


@dataclass(frozen=True, slots=True)
class DomainView:
    id: str
    domain: str
    verified: bool
    txt_name: str
    txt_value: str


@dataclass(frozen=True, slots=True)
class ConnectionView:
    id: str
    protocol: str
    status: str
    default_role: str
    jit_provisioning: bool
    enforced: bool
    config: dict[str, Any]
    login_url: str
    sp_entity_id: str | None = None
    acs_url: str | None = None


class SsoService:
    def __init__(
        self,
        db: Database,
        *,
        envelope: Envelope,
        state: aioredis.Redis,
        identity: IdentityService,
        members: MemberService,
        resolver: TxtResolver,
        http: httpx.AsyncClient,
        api_base_url: str,
        allow_http_localhost: bool = False,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._r = state
        self._identity = identity
        self._members = members
        self._resolver = resolver
        self._http = http
        self._api = api_base_url.rstrip("/")
        self._allow_http = allow_http_localhost

    # ---- URLs ------------------------------------------------------------------------
    def _base(self, connection_id: str) -> str:
        return f"{self._api}/v1/auth/sso/{connection_id}"

    def sp_entity_id(self, connection_id: str) -> str:
        return f"{self._base(connection_id)}/saml/metadata"

    def acs_url(self, connection_id: str) -> str:
        return f"{self._base(connection_id)}/saml/acs"

    def oidc_redirect_uri(self, connection_id: str) -> str:
        return f"{self._base(connection_id)}/oidc/callback"

    # ---- domains ---------------------------------------------------------------------
    @staticmethod
    def _domain_view(d: VerifiedDomain) -> DomainView:
        return DomainView(
            d.id,
            d.domain,
            d.verified_at is not None,
            f"_aura-verify.{d.domain}",
            TXT_PREFIX + d.token,
        )

    async def list_domains(self, account_id: str) -> list[DomainView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(VerifiedDomain).order_by(VerifiedDomain.domain))
            return [self._domain_view(d) for d in rows]

    async def add_domain(self, account_id: str, raw: str, actor_id: str) -> DomainView:
        domain = normalize_domain(raw)
        async with self._db.system() as s:
            existing = await s.scalar(select(VerifiedDomain).where(VerifiedDomain.domain == domain))
            if existing is not None:
                if existing.account_id != account_id:
                    raise Conflict("this domain is claimed by another organization")
                return self._domain_view(existing)
            d = VerifiedDomain(
                id=new_id("dom"),
                account_id=account_id,
                domain=domain,
                token=secrets.token_hex(16),
            )
            s.add(d)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="sso.domain_add",
                target_type="domain",
                target_id=d.id,
                after={"domain": domain},
            )
            return self._domain_view(d)

    async def verify_domain(self, account_id: str, domain_id: str, actor_id: str) -> DomainView:
        async with self._db.tenant(account_id) as s:
            d = await s.get(VerifiedDomain, domain_id)
            if d is None:
                raise NotFound("domain not found")
            if d.verified_at is None:
                records = await self._resolver.txt(f"_aura-verify.{d.domain}")
                if TXT_PREFIX + d.token not in records:
                    raise ValidationFailed(
                        f"TXT record not found yet: add _aura-verify.{d.domain} = "
                        f"{TXT_PREFIX}{d.token} (DNS can take a few minutes)"
                    )
                d.verified_at = datetime.now(UTC)
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_id,
                    account_id=account_id,
                    action="sso.domain_verified",
                    target_type="domain",
                    target_id=d.id,
                    after={"domain": d.domain},
                )
            return self._domain_view(d)

    async def _verified_domains(self, account_id: str) -> set[str]:
        async with self._db.system() as s:
            rows = await s.scalars(
                select(VerifiedDomain.domain).where(
                    VerifiedDomain.account_id == account_id,
                    VerifiedDomain.verified_at.is_not(None),
                )
            )
            return set(rows)

    # ---- connections -----------------------------------------------------------------
    def _connection_view(self, c: SsoConnection) -> ConnectionView:
        return ConnectionView(
            c.id,
            c.protocol,
            c.status,
            c.default_role,
            c.jit_provisioning,
            c.enforced,
            dict(c.config),
            f"{self._base(c.id)}/start",
            self.sp_entity_id(c.id) if c.protocol == "saml" else None,
            self.acs_url(c.id) if c.protocol == "saml" else None,
        )

    async def list_connections(self, account_id: str) -> list[ConnectionView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(SsoConnection).order_by(SsoConnection.created_at))
            return [self._connection_view(c) for c in rows]

    @staticmethod
    def _check_role(role: str) -> str:
        if role not in ASSIGNABLE_DEFAULT_ROLES:
            raise ValidationFailed("default role must be a non-owner role")
        return role

    async def create_oidc(
        self,
        account_id: str,
        actor_id: str,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        default_role: str = "viewer",
        jit_provisioning: bool = True,
    ) -> ConnectionView:
        if not client_id or not client_secret:
            raise ValidationFailed("client_id and client_secret are required")
        config = await oidc.discover(self._http, issuer, allow_http_localhost=self._allow_http)
        config["client_id"] = client_id
        return await self._create(
            account_id,
            actor_id,
            "oidc",
            config,
            self._check_role(default_role),
            jit_provisioning,
            secret=client_secret,
        )

    async def create_saml(
        self,
        account_id: str,
        actor_id: str,
        *,
        idp_entity_id: str,
        idp_sso_url: str,
        idp_certificate_pem: str,
        default_role: str = "viewer",
        jit_provisioning: bool = True,
    ) -> ConnectionView:
        from cryptography import x509

        oidc.check_url(idp_sso_url, allow_http_localhost=self._allow_http)
        if not idp_entity_id.strip():
            raise ValidationFailed("idp_entity_id is required")
        try:
            x509.load_pem_x509_certificate(idp_certificate_pem.encode())
        except ValueError as exc:
            raise ValidationFailed("idp_certificate_pem is not a valid PEM certificate") from exc
        config = {
            "idp_entity_id": idp_entity_id.strip(),
            "idp_sso_url": idp_sso_url,
            "idp_certificate_pem": idp_certificate_pem.strip(),
        }
        return await self._create(
            account_id, actor_id, "saml", config, self._check_role(default_role), jit_provisioning
        )

    async def _create(
        self,
        account_id: str,
        actor_id: str,
        protocol: str,
        config: dict[str, Any],
        default_role: str,
        jit: bool,
        *,
        secret: str | None = None,
    ) -> ConnectionView:
        conn = SsoConnection(
            id=new_id("sso"),
            account_id=account_id,
            protocol=protocol,
            status="draft",
            config=config,
            default_role=default_role,
            jit_provisioning=jit,
            enforced=False,
        )
        if secret is not None:
            sealed = self._envelope.seal(secret, aad=_aad(account_id, conn.id))
            conn.key_id, conn.wrapped_dek = sealed.key_id, sealed.wrapped_dek
            conn.nonce, conn.ciphertext = sealed.nonce, sealed.ciphertext
        async with self._db.tenant(account_id) as s:
            s.add(conn)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="sso.connection_create",
                target_type="sso_connection",
                target_id=conn.id,
                after={"protocol": protocol, "default_role": default_role},
            )
        return self._connection_view(conn)

    async def update_connection(
        self,
        account_id: str,
        connection_id: str,
        actor_id: str,
        *,
        status: str | None = None,
        enforced: bool | None = None,
        default_role: str | None = None,
        jit_provisioning: bool | None = None,
    ) -> ConnectionView:
        async with self._db.tenant(account_id) as s:
            conn = await s.get(SsoConnection, connection_id)
            if conn is None:
                raise NotFound("connection not found")
            before = {"status": conn.status, "enforced": conn.enforced}
            if status is not None:
                if status not in ("draft", "active", "disabled"):
                    raise ValidationFailed("status must be draft, active or disabled")
                conn.status = status
            if default_role is not None:
                conn.default_role = self._check_role(default_role)
            if jit_provisioning is not None:
                conn.jit_provisioning = jit_provisioning
            if enforced is not None:
                conn.enforced = enforced
            if conn.enforced and conn.status != "active":
                raise ValidationFailed("only an active connection can be enforced")
            if conn.status == "active" and not await self._verified_domains(account_id):
                raise ValidationFailed("verify at least one domain before activating SSO")
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="sso.connection_update",
                target_type="sso_connection",
                target_id=conn.id,
                before=before,
                after={"status": conn.status, "enforced": conn.enforced},
            )
            return self._connection_view(conn)

    async def delete_connection(self, account_id: str, connection_id: str, actor_id: str) -> None:
        async with self._db.tenant(account_id) as s:
            conn = await s.get(SsoConnection, connection_id)
            if conn is None:
                raise NotFound("connection not found")
            await s.delete(conn)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="sso.connection_delete",
                target_type="sso_connection",
                target_id=connection_id,
            )

    # ---- discovery and enforcement ---------------------------------------------------
    async def _active_for_domain(self, domain: str) -> SsoConnection | None:
        async with self._db.system() as s:
            return await s.scalar(
                select(SsoConnection)
                .join(VerifiedDomain, VerifiedDomain.account_id == SsoConnection.account_id)
                .where(
                    VerifiedDomain.domain == domain,
                    VerifiedDomain.verified_at.is_not(None),
                    SsoConnection.status == "active",
                )
                .order_by(SsoConnection.enforced.desc(), SsoConnection.created_at)
                .limit(1)
            )

    async def discover(self, raw_email: str) -> str | None:
        """Login page helper: the SSO start URL for this email's domain, if any."""
        conn = await self._active_for_domain(_domain_of(normalize_email(raw_email)))
        return f"{self._base(conn.id)}/start" if conn else None

    async def login_guard(self, email: str) -> None:
        """Registered on IdentityService: refuses non-SSO sign-in for enforced domains."""
        conn = await self._active_for_domain(_domain_of(email))
        if conn is not None and conn.enforced:
            raise Forbidden("your organization requires single sign-on; use 'Sign in with SSO'")

    # ---- login -----------------------------------------------------------------------
    async def _connection(self, connection_id: str) -> SsoConnection:
        async with self._db.system() as s:
            conn = await s.get(SsoConnection, connection_id)
        if conn is None or conn.status != "active":
            raise NotFound("single sign-on is not available")
        return conn

    def _state_key(self, state: str) -> str:
        return build_key(KeyFamily.OAUTH_STATE, GLOBAL, f"sso_{state}")

    async def start(
        self, connection_id: str, *, return_to: str = "/", login_hint: str | None = None
    ) -> str:
        conn = await self._connection(connection_id)
        state = secrets.token_urlsafe(24)
        safe_return = (
            return_to
            if return_to.startswith("/") and "//" not in return_to and "\\" not in return_to
            else "/"
        )
        data: dict[str, str] = {"c": conn.id, "r": safe_return}
        if conn.protocol == "oidc":
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            data["v"] = verifier
            url = oidc.OidcClient(conn.config, "", self._http).authorization_url(
                state=state,
                code_challenge=challenge,
                redirect_uri=self.oidc_redirect_uri(conn.id),
                login_hint=login_hint,
            )
        else:
            request_id = "_" + secrets.token_hex(16)  # XML IDs may not start with a digit
            data["rid"] = request_id
            url = saml.authn_request_url(
                idp_sso_url=conn.config["idp_sso_url"],
                sp_entity_id=self.sp_entity_id(conn.id),
                acs_url=self.acs_url(conn.id),
                request_id=request_id,
                relay_state=state,
            )
        await self._r.set(
            self._state_key(state),
            json.dumps(data),
            ex=ttl_for(KeyFamily.OAUTH_STATE, jitter=False),
        )
        return url

    async def _take_state(self, connection_id: str, state: str) -> dict[str, str]:
        raw = await self._r.getdel(self._state_key(state))
        if raw is None:
            raise Unauthenticated("sign-in session expired; start again")
        data: dict[str, str] = json.loads(raw)
        if data["c"] != connection_id:
            raise Unauthenticated("sign-in session does not match this connection")
        return data

    async def finish_oidc(
        self, connection_id: str, *, code: str, state: str
    ) -> tuple[UserView, str]:
        data = await self._take_state(connection_id, state)
        conn = await self._connection(connection_id)
        secret = self._envelope.open(
            Sealed(
                conn.key_id or "",
                conn.wrapped_dek or b"",
                conn.nonce or b"",
                conn.ciphertext or b"",
            ),
            aad=_aad(conn.account_id, conn.id),
        )
        info = await oidc.OidcClient(conn.config, secret, self._http).exchange(
            code=code, code_verifier=data["v"], redirect_uri=self.oidc_redirect_uri(conn.id)
        )
        user = await self._sign_in(conn, subject=info.subject, email=info.email, name=info.name)
        return user, data["r"]

    async def finish_saml(
        self, connection_id: str, *, saml_response: str, relay_state: str
    ) -> tuple[UserView, str]:
        data = await self._take_state(connection_id, relay_state)
        conn = await self._connection(connection_id)
        ident = saml.parse_response(
            saml_response,
            idp_entity_id=conn.config["idp_entity_id"],
            idp_cert_pem=conn.config["idp_certificate_pem"],
            sp_entity_id=self.sp_entity_id(conn.id),
            acs_url=self.acs_url(conn.id),
            expected_request_id=data["rid"],
        )
        # One-time use: a captured assertion cannot be replayed.
        ttl_s = max(60, int((ident.not_on_or_after - datetime.now(UTC)).total_seconds()) + 300)
        fresh = await self._r.set(
            build_key(
                KeyFamily.OAUTH_STATE,
                GLOBAL,
                f"saml_{hashlib.sha256(ident.assertion_id.encode()).hexdigest()[:40]}",
            ),
            "1",
            nx=True,
            ex=ttl_s,
        )
        if not fresh:
            raise Unauthenticated("this SAML assertion was already used")
        user = await self._sign_in(conn, subject=ident.name_id, email=ident.email, name=ident.name)
        return user, data["r"]

    async def _sign_in(
        self, conn: SsoConnection, *, subject: str, email: str, name: str | None
    ) -> UserView:
        email = normalize_email(email)
        if _domain_of(email) not in await self._verified_domains(conn.account_id):
            raise Forbidden("this identity provider cannot sign in users from that email domain")
        user = await self._identity.login_with_oauth(
            provider=f"sso:{conn.id}",
            subject=subject,
            email=email,
            email_verified=True,  # the organization proved it owns the domain
            name=name,
        )
        async with self._db.system() as s:
            org_id = await s.scalar(
                select(Organization.id).where(Organization.account_id == conn.account_id)
            )
            member = await s.scalar(
                select(Membership.id).where(
                    Membership.organization_id == org_id, Membership.user_id == user.id
                )
            )
        if member is None:
            if not conn.jit_provisioning:
                raise Forbidden("you have not been given access; ask your administrator")
            await self._members.add_member(
                None,
                f"sso:{conn.id}",
                conn.account_id,
                email=email,
                role=conn.default_role,
                name=name,
                source="sso",
            )
        return user
