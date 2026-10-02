"""Typed error hierarchy. Every error maps to an RFC 7807 problem document."""

from __future__ import annotations

from typing import Any, ClassVar


class AuraError(Exception):
    """Base class for all expected AURA errors."""

    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    title: ClassVar[str] = "Internal error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"https://docs.aura.ai/errors/{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class ValidationFailed(AuraError):
    status, code, title = 422, "validation_failed", "Validation failed"


class Unauthenticated(AuraError):
    status, code, title = 401, "unauthenticated", "Authentication required"


class Forbidden(AuraError):
    status, code, title = 403, "forbidden", "Not allowed"


class NotFound(AuraError):
    status, code, title = 404, "not_found", "Resource not found"


class Conflict(AuraError):
    status, code, title = 409, "conflict", "Conflict"


class RateLimited(AuraError):
    status, code, title = 429, "rate_limited", "Rate limit exceeded"


class QuotaExceeded(AuraError):
    status, code, title = 402, "quota_exceeded", "Quota or credit limit exceeded"


class BudgetExceeded(AuraError):
    status, code, title = 402, "budget_exceeded", "Execution budget exceeded"


class DependencyUnavailable(AuraError):
    status, code, title = 503, "dependency_unavailable", "A dependency is unavailable"
