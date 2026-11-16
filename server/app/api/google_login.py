"""Sign in with Google routes (`auth` tag; T6.0, contracts/NOTES.md D26). Thin: the logic is
in app.auth.google_signin. Both routes are unauthenticated and, like POST /auth/apple, need
no X-Timezone (D11). They live here rather than in app.auth.router because they compose the
auth runtime with the gcal runtime and the shared sessionmaker, as app.api.gcal does.

Neither route holds a request-scoped transaction: /start touches no DB, and /complete
exchanges the code with Google before it opens its one short transaction.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.api.deps import Maker, RawBody, parse_body
from app.api.gcal import GcalConnectUrlOut, get_gcal_runtime
from app.auth.dependencies import RuntimeDep
from app.auth.google_signin import GoogleSignInService
from app.auth.router import session_out, user_out
from app.auth.schemas import AuthSessionOut
from app.gcal.runtime import GcalRuntime
from app.gcal.state import NONCE_HASH_RE

router = APIRouter(tags=["auth"])


class GoogleSignInStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # base64url(SHA-256(nonce)), unpadded (43 chars).
    nonce_hash: str = Field(pattern=NONCE_HASH_RE.pattern)


class GoogleSignInCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_code: str = Field(min_length=1)
    state: str = Field(min_length=1)
    nonce: str = Field(min_length=16, max_length=512)


class GoogleAuthSessionOut(AuthSessionOut):
    """POST /auth/apple's AuthSession plus `gcal_connected` (D26)."""

    gcal_connected: bool


_START: TypeAdapter[GoogleSignInStartIn] = TypeAdapter(GoogleSignInStartIn)
_COMPLETE: TypeAdapter[GoogleSignInCompleteIn] = TypeAdapter(GoogleSignInCompleteIn)


def get_google_signin(
    auth: RuntimeDep,
    gcal: Annotated[GcalRuntime, Depends(get_gcal_runtime)],
    maker: Maker,
) -> GoogleSignInService:
    return GoogleSignInService(maker, auth_service=auth.service, gcal=gcal)


SignIn = Annotated[GoogleSignInService, Depends(get_google_signin)]


@router.post(
    "/auth/google/start",
    operation_id="startGoogleSignIn",
    response_model=GcalConnectUrlOut,
)
async def start(
    gcal: Annotated[GcalRuntime, Depends(get_gcal_runtime)], maker: Maker, body: RawBody
) -> GcalConnectUrlOut:
    req = parse_body(_START, body)
    url = gcal.service(maker).login_authorization_url(req.nonce_hash)
    return GcalConnectUrlOut(authorization_url=url)


# Always 200 with is_new_user, like /auth/apple (D21).
@router.post(
    "/auth/google/complete",
    operation_id="completeGoogleSignIn",
    response_model=GoogleAuthSessionOut,
)
async def complete(service: SignIn, body: RawBody) -> GoogleAuthSessionOut:
    req = parse_body(_COMPLETE, body)
    result = await service.complete(auth_code=req.auth_code, state=req.state, nonce=req.nonce)
    signed = result.sign_in
    return GoogleAuthSessionOut(
        token=signed.token,
        session=session_out(signed.session),
        user=user_out(signed.user),
        is_new_user=signed.is_new_user,
        gcal_connected=result.gcal_connected,
    )
