import os
import re
from hmac import compare_digest, digest
from secrets import token_hex
from time import time
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from database import SessionLocal
from models import User
from utils.password import hash_password, verify_password
from web.views import render


router = APIRouter()
SESSION_KEY = os.getenv("READER_SESSION_SECRET", "").encode() or os.urandom(32)
SESSION_COOKIE = "reader_session"
SESSION_AGE = 7 * 24 * 60 * 60


def same_origin(request):
    origin = request.headers.get("origin")
    return request.headers.get("sec-fetch-site") != "cross-site" and (
        not origin or origin == str(request.base_url).rstrip("/")
    )


def session_signature(payload, user):
    return digest(SESSION_KEY, f"{payload}.{user.password_hash}".encode(), "sha256").hex()


def profile(request, session, *, lock=False):
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or len(cookie) > 160:
        return None
    try:
        payload, signature = cookie.rsplit(".", 1)
        identity, expires, _ = payload.split(".")
        if int(expires) <= time():
            return None
        user_id = UUID(identity)
    except ValueError:
        return None
    user = session.get(User, user_id, with_for_update=lock)
    if user and compare_digest(signature.encode(), session_signature(payload, user).encode()):
        return user


def signed_in(request, user, target):
    payload = f"{user.id_user.hex}.{int(time()) + SESSION_AGE}.{token_hex(8)}"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(SESSION_COOKIE, f"{payload}.{session_signature(payload, user)}",
                        max_age=SESSION_AGE, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


def signed_out(request, target):
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=request.url.scheme == "https")
    return response


def sign_in_required(request, target=None):
    if target is None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
    return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)


def account_target(target):
    path = target.partition("?")[0]
    return target if path in ("/", "/account", "/library", "/history") or re.fullmatch(r"/books/\d+", path) else "/account"


def auth_page(request, target, **context):
    registering = request.url.path == "/register"
    return render(request, "auth", registering=registering, target=account_target(target), **context)


@router.get("/login")
@router.get("/register")
def auth(request: Request, next: str = "/account", deleted: bool = False):
    return auth_page(request, next, notice="Account deleted." if deleted else None)


@router.post("/login")
@router.post("/register")
def authenticate(request: Request, username: str = Form(""), password: str = Form(""),
                 confirm_password: str = Form(""), next: str = Form("/account")):
    username = username.strip()
    if not same_origin(request):
        return auth_page(request, next, status_code=403, error="This request was rejected.")
    if request.url.path == "/login":
        if 0 < len(username) <= 50 and 0 < len(password) <= 256:
            with SessionLocal() as session:
                user = session.scalar(select(User).where(User.username == username))
                if user and verify_password(user.password_hash, password):
                    return signed_in(request, user, account_target(next))
        return auth_page(request, next, status_code=401, error="The username or password is incorrect.", username=username)

    if not 3 <= len(username) <= 50 or not 8 <= len(password) <= 256:
        return auth_page(request, next, status_code=400, username=username,
                         error="Use 3–50 characters for your username and 8–256 for your password.")
    if password != confirm_password:
        return auth_page(request, next, status_code=400, error="The passwords must match.", username=username)
    password_hash = hash_password(password)
    if not password_hash:
        return auth_page(request, next, status_code=503, error="The account could not be created. Try again.", username=username)
    with SessionLocal.begin() as session:
        user = session.scalar(
            insert(User).values(username=username, password_hash=password_hash)
            .on_conflict_do_nothing(index_elements=[User.username]).returning(User)
        )
    if not user:
        return auth_page(request, next, status_code=409, error="That username is already in use.", username=username)
    return signed_in(request, user, account_target(next))


@router.post("/logout")
def logout(request: Request):
    if not same_origin(request):
        return auth_page(request, "/account", status_code=403, error="This request was rejected.")
    return signed_out(request, "/login")
