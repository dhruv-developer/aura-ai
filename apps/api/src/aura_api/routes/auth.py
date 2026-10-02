from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from aura_api.container import Container
from aura_api.deps import ContainerDep, PrincipalDep, UserDep
from aura_api.schemas import MagicLinkRequest, MagicLinkVerify, UserOut
from aura_auth.application.dto import UserView

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _start_session(
    c: Container, request: Request, response: Response, user: UserView
) -> None:
    cookie = await c.sessions.create(
        user.id, ip=_client_ip(request), user_agent=request.headers.get("user-agent")
    )
    response.set_cookie(
        c.settings.session_cookie_name,
        cookie,
        max_age=c.settings.session_ttl_s,
        httponly=True,
        secure=c.settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/magic-link", status_code=202)
async def request_magic_link(
    body: MagicLinkRequest, request: Request, c: ContainerDep
) -> dict[str, str]:
    # Same response whether or not the email is registered (no account enumeration).
    await c.magic_links.request(body.email, ip=_client_ip(request), app=body.app)
    return {"status": "sent"}


@router.post("/magic-link/verify", response_model=UserOut)
async def verify_magic_link(
    body: MagicLinkVerify, request: Request, response: Response, c: ContainerDep
) -> UserView:
    user = await c.magic_links.verify(body.token)
    await _start_session(c, request, response, user)
    return user


@router.get("/{provider}/start")
async def oauth_start(provider: str, c: ContainerDep, return_to: str = "/") -> RedirectResponse:
    url = await c.oauth.start(provider, return_to=return_to)
    return RedirectResponse(url, status_code=302)


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str, code: str, state: str, request: Request, c: ContainerDep
) -> RedirectResponse:
    user, return_to = await c.oauth.finish(provider, code=code, state=state)
    response = RedirectResponse(
        f"{c.settings.public_web_url.rstrip('/')}{return_to}", status_code=302
    )
    await _start_session(c, request, response, user)
    return response


@router.post("/logout", status_code=204)
async def logout(principal: PrincipalDep, c: ContainerDep, response: Response) -> Response:
    if principal.session_cookie:
        await c.sessions.revoke(principal.session_cookie)
    response.delete_cookie(c.settings.session_cookie_name, path="/")
    response.status_code = 204
    return response


@router.post("/logout-all", status_code=204)
async def logout_all(user: UserDep, c: ContainerDep, response: Response) -> Response:
    await c.sessions.revoke_all(user.id)
    response.delete_cookie(c.settings.session_cookie_name, path="/")
    response.status_code = 204
    return response
