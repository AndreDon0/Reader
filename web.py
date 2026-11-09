"""Local book library and chapter reader. Run with `uv run python web.py`."""

import argparse
import logging
import os
import secrets
import signal
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from flask import Flask, Response, abort, g, render_template, request, session
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

import database
from models import Book, Chapter, DownloadJob, Followed, FollowedStatus, ReadChapter
from reader_web import accounts, admin, catalog, reading
from utils.cbz import IMAGE_TYPES
from utils.chapter_cache import cache_path, cached_pages, cached_text
from utils.custom_request import close_vpn_pools
from utils.logging_config import configure_logging
from utils.jobs import SOURCES, download_site
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def safe_url(value: str | None) -> str | None:
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme in {"http", "https"} and parsed.hostname else None
    except ValueError:
        return None


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("READER_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_FACTORY=None,
        DATA_ROOT=ROOT,
        BOOKS_PER_PAGE=24,
        CHAPTERS_PER_PAGE=100,
        SESSION_COOKIE_SECURE=os.environ.get("READER_HTTPS") == "1",
        TRUSTED_HOSTS=os.environ["READER_TRUSTED_HOSTS"].split(",") if os.environ.get("READER_TRUSTED_HOSTS") else None,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=128 * 1024,
        TRUST_PROXY=os.environ.get("READER_TRUST_PROXY") == "1",
    )
    if config:
        app.config.update(config)
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.jinja_env.filters["safe_url"] = safe_url

    def db():
        if "db" not in g:
            factory = app.config["SESSION_FACTORY"]
            if factory is None:
                if not database.initialize_database():
                    abort(503, description="The library database is unavailable. Check DATABASE_URL and try again.")
                factory = database.SessionLocal
            g.db = factory()
        return g.db

    @app.teardown_appcontext
    def close_session(error=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(24)
        return session["csrf_token"]

    @app.context_processor
    def template_helpers():
        return {"csrf_token": csrf_token}

    def pagination(total: int, size: int):
        pages = max(1, (total + size - 1) // size)
        page = min(pages, max(1, request.args.get("page", 1, type=int)))
        return page, pages, (page - 1) * size

    accounts.register(app, db)

    @app.before_request
    def verify_csrf():
        if request.method == "POST" and request.endpoint is not None:
            provided = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
                abort(400, description="This page expired. Reload it and try again.")

    @app.after_request
    def response_headers(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        db().execute(select(Book.hidden).limit(1))
        return {"ready": True}

    def get_book(book_id):
        book = db().scalar(select(Book).where(Book.id_book == book_id).options(
            joinedload(Book.author), joinedload(Book.site), selectinload(Book.tags),
        ))
        if book is None or book.hidden:
            abort(404, description="This book is not in your library.")
        return book

    def get_chapter(chapter_id):
        chapter = db().scalar(select(Chapter).where(Chapter.id_chapter == chapter_id).options(
            joinedload(Chapter.book).joinedload(Book.site),
        ))
        if chapter is None or chapter.hidden or chapter.book.hidden:
            abort(404, description="This chapter is not in your library.")
        return chapter

    def chapter_view(chapter):
        root = Path(app.config["DATA_ROOT"])
        pages = cached_pages(chapter.file_path, root)
        text = cached_text(chapter.file_path, root)
        if text:
            lines = text.strip().splitlines()
            if lines and lines[0].strip() == chapter.name.strip():
                lines = lines[1:]
            paragraphs = [line.strip() for line in lines if line.strip()]
        else:
            paragraphs = []
        key = tuple_(Chapter.number, Chapter.id_chapter)
        current = tuple_(chapter.number, chapter.id_chapter)
        siblings = select(Chapter).where(Chapter.id_book == chapter.id_book, Chapter.hidden.is_(False))
        previous = db().scalar(siblings.where(key < current).order_by(
            Chapter.number.desc(), Chapter.id_chapter.desc()).limit(1))
        following = db().scalar(siblings.where(key > current).order_by(
            Chapter.number, Chapter.id_chapter).limit(1))
        chapter_position = db().scalar(select(func.count()).select_from(Chapter).where(
            Chapter.id_book == chapter.id_book, Chapter.hidden.is_(False), key <= current,
        ))
        list_page = (chapter_position - 1) // app.config["CHAPTERS_PER_PAGE"] + 1
        saved = db().get(ReadChapter, (g.user.id_user, chapter.id_chapter))
        job = db().get(DownloadJob, chapter.id_chapter)
        return render_template("chapter.html", chapter=chapter, book=chapter.book,
                               paragraphs=paragraphs, pages=pages, previous=previous, following=following,
                               list_page=list_page, supported=download_site(chapter) is not None,
                               saved=saved, view=reading.progress_view(chapter, len(pages or paragraphs)), job=job)

    catalog.register(app, db, pagination)
    reading.register(app, db, get_book, get_chapter)
    admin.register(app, db, pagination, safe_url)

    @app.get("/books/<int:book_id>")
    def book_detail(book_id):
        book = get_book(book_id)
        visible = (Chapter.id_book == book_id, Chapter.hidden.is_(False))
        total = db().scalar(select(func.count()).select_from(Chapter).where(*visible))
        ready = db().scalar(select(func.count()).select_from(Chapter).where(*visible, Chapter.file_path.is_not(None)))
        page, pages, offset = pagination(total, app.config["CHAPTERS_PER_PAGE"])
        chapters = db().execute(select(Chapter, DownloadJob).outerjoin(DownloadJob)
                                .where(*visible).order_by(Chapter.number, Chapter.id_chapter)
                                .limit(app.config["CHAPTERS_PER_PAGE"]).offset(offset)).all()
        first = db().scalar(select(Chapter.id_chapter).where(*visible)
                            .order_by(Chapter.number, Chapter.id_chapter).limit(1))
        resume = db().scalar(select(ReadChapter.id_chapter).join(Chapter)
                             .where(*visible, ReadChapter.id_user == g.user.id_user)
                             .order_by(ReadChapter.read_at.desc()).limit(1))
        follow = db().get(Followed, (g.user.id_user, book_id))
        site = SOURCES.get(book.site.name)
        return render_template("book.html", book=book, chapters=chapters, total=total, ready=ready,
                               first=first, resume=resume, follow=follow, statuses=FollowedStatus,
                               supported=site and site.supports_downloads, page=page, pages=pages)

    @app.get("/chapters/<int:chapter_id>")
    def read_chapter(chapter_id):
        return chapter_view(get_chapter(chapter_id))

    @app.get("/chapters/<int:chapter_id>/pages/<name>")
    def comic_page(chapter_id, name):
        chapter = get_chapter(chapter_id)
        path = cache_path(chapter.file_path, Path(app.config["DATA_ROOT"]), ".cbz")
        mime = IMAGE_TYPES.get(Path(name).suffix.lower())
        if path is None or mime is None or '\\' in name or name in {'.', '..'}:
            abort(404)
        try:
            with ZipFile(path) as archive:
                return Response(archive.read(name), mimetype=mime, headers={"X-Content-Type-Options": "nosniff"})
        except (OSError, BadZipFile, KeyError, RuntimeError, NotImplementedError):
            abort(404)

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        logger.error("Library database request failed (%s)", type(error).__name__)
        return render_template("error.html", code=503,
                               message="The library database is unavailable. Check its connection and try again."), 503

    for code in (400, 403, 404, 413, 422, 503):
        def render_error(error):
            return render_template("error.html", code=error.code, message=error.description), error.code
        app.register_error_handler(code, render_error)
    return app


def main():
    parser = argparse.ArgumentParser(description="Browse the Reader database in a local web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    configure_logging()
    try:
        create_app().run(host=args.host, port=args.port, debug=False, use_reloader=False)
    finally:
        close_vpn_pools()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        pass
