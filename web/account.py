from typing import Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from database import SessionLocal
from models import Chapter, Language, ReadChapter, user_primary_language, user_secondary_language
from utils.password import hash_password, verify_password
from web.auth import profile, same_origin, sign_in_required, signed_in, signed_out
from web.views import render


router = APIRouter()


def account_page(request, session, user, **context):
    if not user:
        return render(request, "account", user=None, **context)
    primary = session.scalar(
        select(user_primary_language.c.language).where(user_primary_language.c.id_user == user.id_user)
        .order_by(user_primary_language.c.language).limit(1)
    )
    secondary = set(session.scalars(
        select(user_secondary_language.c.language).where(user_secondary_language.c.id_user == user.id_user)
    ))
    primary = primary or next((language for language in Language if language not in secondary), Language.ENGLISH)
    books, chapters = session.execute(
        select(func.count(Chapter.id_book.distinct()), func.count())
        .join(ReadChapter, ReadChapter.id_chapter == Chapter.id_chapter)
        .where(ReadChapter.id_user == user.id_user)
    ).one()
    return render(request, "account", user=user, books=books, chapters=chapters, languages=Language,
                  primary=primary, secondary=secondary - {primary}, **context)


@router.get("/account")
def account(request: Request, updated: str = ""):
    with SessionLocal() as session:
        user = profile(request, session)
        if not user:
            return sign_in_required(request)
        return account_page(request, session, user, notice={
            "languages": "Languages saved.",
            "password": "Password changed.",
        }.get(updated))


@router.post("/account/{action}")
def update_account(
    request: Request, action: Literal["languages", "password", "delete"],
    primary: Language | None = Form(None), secondary: list[Language] | None = Form(None),
    current_password: str = Form(""), new_password: str = Form(""),
    confirm_delete: bool = Form(False),
):
    if not same_origin(request):
        return render(request, "account", status_code=403, user=None)
    with SessionLocal.begin() as session:
        user = profile(request, session, lock=True)
        if not user:
            return RedirectResponse("/login?next=/account", status_code=303)

        if action == "languages":
            chosen = set(secondary or ())
            if primary is None or primary in chosen:
                return account_page(request, session, user, status_code=400,
                                    error="Choose a primary language outside your secondary languages.")
            for table, choices in ((user_primary_language, {primary}), (user_secondary_language, chosen)):
                session.execute(table.delete().where(table.c.id_user == user.id_user))
                if choices:
                    session.execute(table.insert(), [{"id_user": user.id_user, "language": choice} for choice in choices])
        else:
            error = None
            if len(current_password) > 256 or not verify_password(user.password_hash, current_password):
                error = "The current password is incorrect."
            elif action == "delete":
                if confirm_delete:
                    session.delete(user)
                else:
                    error = "Confirm that you want to delete this account."
            elif not 8 <= len(new_password) <= 256 or new_password == current_password:
                error = "Choose a different password with 8–256 characters."
            elif password_hash := hash_password(new_password):
                user.password_hash = password_hash
            else:
                error = "The password could not be changed. Try again."
            if error:
                return account_page(request, session, user, status_code=400, error=error, failed_action=action)
    if action == "password":
        return signed_in(request, user, "/account?updated=password")
    if action == "delete":
        return signed_out(request, "/login?deleted=true")
    return RedirectResponse("/account?updated=languages", status_code=303)
