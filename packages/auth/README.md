# aura-auth

**Responsibility:** who is calling and what they may do.

- `domain/`: pure logic with no I/O.
  - `rbac`: roles, permissions, API-key grantable permissions, escalation checks.
  - `api_keys`: key format, peppered HMAC hashing, constant-time verification.
- `application/`:
  - `IdentityService`: signup/login by email or OAuth, personal accounts, organizations, access resolution.
  - `SessionStore`: opaque Redis sessions with sliding expiry and "log out everywhere".
  - `MagicLinkService`: single-use, rate-limited login links.
  - `ApiKeyService`: issue, revoke, and authenticate keys, with a 60 s cache bounding revocation lag.
- `adapters/`: SMTP and in-memory mailers, Google OIDC client.

Account creation calls `AccountCreatedHook`s (billing registers one to start the default subscription). This keeps auth independent of billing.

**Does not do:** plans, entitlements, or payments (`aura-billing`), or HTTP routing (`apps/api`).
