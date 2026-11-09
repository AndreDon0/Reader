import hashlib
import re
import time
from functools import lru_cache
from getpass import getpass
from urllib.parse import urlsplit
from uuid import UUID

import click
from flask import abort, g, redirect, render_template, request, session, url_for
from sqlalchemy import case, delete, func, select

from models import AuthAttempt, User
from utils.db_write import upsert
from utils.password import hash_password, verify_password

USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,49}")


@lru_cache(maxsize=1)
def _dummy_hash():
    return hash_password("dummy hash for unknown Reader account")


def _safe_next(target):
    try:
        parsed = urlsplit(target)
        if (target.startswith("/") and not target.startswith("//") and "\\" not in target
                and not parsed.netloc and all(ord(character) >= 32 for character in target)):
            return target
    except ValueError:
        pass
    return url_for("library")


def register(app, db):
    @app.before_request
    def identify_user():
        g.user = None
        if request.endpoint in {None, "static", "health"}:
            return
        account_id = session.get("user")
        if account_id:
            try:
                g.user = db().get(User, UUID(account_id))
            except (ValueError, TypeError):
                pass
            if (g.user is None or g.user.disabled or
                    g.user.session_version != session.get("session_version")):
                session.clear()
                g.user = None
        if g.user is None and request.endpoint not in {"login", "signup"}:
            target = request.full_path if request.method == "GET" else request.path
            return redirect(url_for("login", next=target.rstrip("?")))

    def allow_attempt(kind, limit):
        now = int(time.time()) // 600
        ip = hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()
        insert = upsert(db(), AuthAttempt).values(key=f"{kind}:{ip}", window=now, count=1)
        count = db().scalar(insert.on_conflict_do_update(
            index_elements=["key"], set_={
                "window": now,
                "count": case((AuthAttempt.window == now, AuthAttempt.count + 1), else_=1),
            },
        ).returning(AuthAttempt.count))
        db().execute(delete(AuthAttempt).where(AuthAttempt.window < now - 144))
        db().commit()
        return count <= limit

    def sign_in(user):
        session.clear()
        session.permanent = True
        session.update(user=str(user.id_user), session_version=user.session_version)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = _safe_next(request.values.get("next", ""))
        if g.user:
            return redirect(target)
        if request.method == "POST":
            if not allow_attempt("login", 12):
                return render_template("auth.html", mode="login", target=target,
                                       error="Too many attempts. Try again in a few minutes."), 429
            username = request.form.get("username", "").strip().lower()[:50]
            password = request.form.get("password", "")
            user = db().scalar(select(User).where(func.lower(User.username) == username)
                               .order_by(User.username).limit(1))
            stored = user.password_hash if user else _dummy_hash()
            if len(password) <= 1024 and stored and verify_password(stored, password) and user and not user.disabled:
                sign_in(user)
                return redirect(target, code=303)
            return render_template("auth.html", mode="login", target=target,
                                   error="Invalid username or password."), 401
        return render_template("auth.html", mode="login", target=target)

    @app.route("/register", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("library"))
        if request.method == "POST":
            if not allow_attempt("register", 5):
                return render_template("auth.html", mode="register",
                                       error="Too many attempts. Try again in a few minutes."), 429
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            if not USERNAME.fullmatch(username) or not 12 <= len(password) <= 1024:
                return render_template("auth.html", mode="register",
                                       error="Use a username with 3–50 letters, numbers, dots, dashes or underscores, and a password of at least 12 characters."), 400
            if db().scalar(select(User.id_user).where(func.lower(User.username) == username).limit(1)):
                return render_template("auth.html", mode="register", error="That username is taken."), 409
            stored = hash_password(password)
            if stored is None:
                abort(503)
            insert = upsert(db(), User).values(username=username, password_hash=stored)
            user = db().scalar(insert.on_conflict_do_nothing(index_elements=["username"]).returning(User))
            if user is None:
                return render_template("auth.html", mode="register", error="That username is taken."), 409
            db().commit()
            sign_in(user)
            return redirect(url_for("library"), code=303)
        return render_template("auth.html", mode="register")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.cli.command("create-admin")
    @click.argument("username")
    def create_admin(username):
        """Create a new Reader administrator. The password is read from the terminal."""
        username = username.strip().lower()
        if not USERNAME.fullmatch(username):
            raise click.ClickException("Use a valid username with 3–50 characters.")
        if db().scalar(select(User.id_user).where(func.lower(User.username) == username).limit(1)):
            raise click.ClickException("That username already exists.")
        password = getpass("Admin password: ")
        if not 12 <= len(password) <= 1024 or password != getpass("Repeat password: "):
            raise click.ClickException("Passwords must match and contain at least 12 characters.")
        stored = hash_password(password)
        if stored is None:
            raise click.ClickException("Could not hash the password.")
        insert = upsert(db(), User).values(username=username, password_hash=stored, is_admin=True)
        user_id = db().scalar(insert.on_conflict_do_nothing(index_elements=["username"]).returning(User.id_user))
        if user_id is None:
            raise click.ClickException("That username already exists.")
        db().commit()
        click.echo(f"Created admin {username}.")
