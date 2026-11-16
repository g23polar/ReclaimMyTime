"""Auth routes per contracts/openapi.yaml (`auth` tag)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.auth.dependencies import (
    CurrentSession,
    CurrentUser,
    DeletionServiceDep,
    ServiceDep,
    StepwiseCurrentUser,
    require_apple_configured,
    require_timezone,
)
from app.auth.repository import SessionRecord, UserRecord
from app.auth.schemas import AppleSignInRequest, AuthSessionOut, SessionOut, UserOut

router = APIRouter(tags=["auth"])


def user_out(user: UserRecord) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


def session_out(session: SessionRecord) -> SessionOut:
    return SessionOut(id=session.id, created_at=session.created_at, expires_at=session.expires_at)


# D21: always 200 (never 201), with is_new_user. X-Timezone not required here (D11).
# T6.0 (D26): Apple is optional; unconfigured → 503 apple_unavailable before anything else.
@router.post(
    "/auth/apple",
    operation_id="signInWithApple",
    response_model=AuthSessionOut,
    dependencies=[Depends(require_apple_configured)],
)
async def sign_in_with_apple(body: AppleSignInRequest, service: ServiceDep) -> AuthSessionOut:
    result = await service.sign_in_with_apple(
        body.identity_token, body.authorization_code, body.nonce
    )
    return AuthSessionOut(
        token=result.token,
        session=session_out(result.session),
        user=user_out(result.user),
        is_new_user=result.is_new_user,
    )


@router.get(
    "/me",
    operation_id="getMe",
    response_model=UserOut,
    dependencies=[Depends(require_timezone)],
)
async def get_me(user: CurrentUser) -> UserOut:
    return user_out(user)


@router.delete(
    "/me",
    operation_id="deleteMe",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_timezone)],
)
async def delete_me(user: StepwiseCurrentUser, deletion: DeletionServiceDep) -> Response:
    # Per-step transactions: no transaction is open while Apple revokes the credential.
    await deletion.delete_account(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/sessions",
    operation_id="revokeAllSessions",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_timezone)],
)
async def revoke_all_sessions(user: CurrentUser, service: ServiceDep) -> Response:
    await service.revoke_all_sessions(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/sessions/current",
    operation_id="revokeCurrentSession",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_timezone)],
)
async def revoke_current_session(session: CurrentSession, service: ServiceDep) -> Response:
    await service.revoke_session(session.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
