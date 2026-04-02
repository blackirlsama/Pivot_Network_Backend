from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models.identity import User
from app.schemas.auth import AccessTokenResponse, LoginRequest, RegisterRequest, UserResponse
from app.services.auth import authenticate_user, create_user, get_user_by_email, issue_session_token

router = APIRouter(prefix="/auth")


def _serialize_user(user: User) -> UserResponse:
    seller_status = user.seller_profile.status if user.seller_profile is not None else "missing"
    buyer_status = user.buyer_wallet.status if user.buyer_wallet is not None else "missing"
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        seller_status=seller_status,
        buyer_status=buyer_status,
        created_at=user.created_at,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_seller(payload: RegisterRequest, db: Session = Depends(get_db)) -> UserResponse:
    if get_user_by_email(db, payload.email) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists.")
    return _serialize_user(create_user(db, payload.email, payload.password, payload.display_name))


@router.post("/login", response_model=AccessTokenResponse)
def login_seller(payload: LoginRequest, db: Session = Depends(get_db)) -> AccessTokenResponse:
    user = authenticate_user(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    user_response = _serialize_user(user)
    session_token = issue_session_token(db, user)
    return AccessTokenResponse(access_token=session_token.token, user=user_response)


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)) -> UserResponse:
    return _serialize_user(current_user)
