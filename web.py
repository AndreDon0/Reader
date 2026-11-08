"""Local book library and chapter reader. Run with `uv run python web.py`."""

import argparse
import logging
import os
import secrets
import signal
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

import database
from models import Author, Book, BookAltTitle, BookStatus, Chapter
from sites.novgo import Novgo
from utils.custom_request import close_vpn_pools
from utils.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent
LOAD_LOCK = Lock()
logger = logging.getLogger(__name__)


def cached_text(file_path: str | None, root: Path) -> str | None:
    """Read only chapter files within data/, including after symlink resolution."""
    if not file_path:
        return None
    try:
        path = (root / file_path).resolve()
        if not path.is_relative_to((root / "data").resolve()):
            return None
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None
    except (OSError, ValueError, UnicodeError) as exc:
        logger.warning("Cannot read cached chapter (%s)", type(exc).__name__)
        return None


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
        CHAPTER_LOADER=None,
    )
    if config:
        app.config.update(config)
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

    def get_book(book_id):
        book = db().scalar(select(Book).where(Book.id_book == book_id).options(
            joinedload(Book.author), joinedload(Book.site), selectinload(Book.tags),
        ))
        if book is None:
            abort(404, description="This book is not in your library.")
        return book

    def get_chapter(chapter_id):
        chapter = db().scalar(select(Chapter).where(Chapter.id_chapter == chapter_id).options(
            joinedload(Chapter.book).joinedload(Book.site),
        ))
        if chapter is None:
            abort(404, description="This chapter is not in your library.")
        return chapter

    def chapter_view(chapter, *, error=None, status=200):
        text = cached_text(chapter.file_path, Path(app.config["DATA_ROOT"]))
        if text:
            lines = text.strip().splitlines()
            if lines and lines[0].strip() == chapter.name.strip():
                lines = lines[1:]
            paragraphs = [line.strip() for line in lines if line.strip()]
        else:
            paragraphs = []
        key = tuple_(Chapter.number, Chapter.id_chapter)
        current = tuple_(chapter.number, chapter.id_chapter)
        siblings = select(Chapter).where(Chapter.id_book == chapter.id_book)
        previous = db().scalar(siblings.where(key < current).order_by(
            Chapter.number.desc(), Chapter.id_chapter.desc()).limit(1))
        following = db().scalar(siblings.where(key > current).order_by(
            Chapter.number, Chapter.id_chapter).limit(1))
        chapter_position = db().scalar(select(func.count()).select_from(Chapter).where(
            Chapter.id_book == chapter.id_book, key <= current,
        ))
        list_page = (chapter_position - 1) // app.config["CHAPTERS_PER_PAGE"] + 1
        supported = chapter.book.site.name == Novgo.name
        return render_template("chapter.html", chapter=chapter, book=chapter.book,
                               paragraphs=paragraphs, previous=previous, following=following,
                               list_page=list_page, supported=supported, error=error), status

    @app.get("/")
    def library():
        q = request.args.get("q", "").strip()[:200]
        status = request.args.get("status", "")
        filters = []
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            filters.append(or_(
                Book.title.ilike(pattern, escape="\\"),
                Book.author.has(Author.nickname.ilike(pattern, escape="\\")),
                Book.alt_titles.any(BookAltTitle.title.ilike(pattern, escape="\\")),
            ))
        if status in {item.value for item in BookStatus}:
            filters.append(Book.status == BookStatus(status))
        else:
            status = ""
        total = db().scalar(select(func.count()).select_from(Book).where(*filters))
        page, pages, offset = pagination(total, app.config["BOOKS_PER_PAGE"])
        books = db().scalars(select(Book).where(*filters).options(
            joinedload(Book.author), joinedload(Book.site),
        ).order_by(Book.id_book.desc()).limit(app.config["BOOKS_PER_PAGE"]).offset(offset)).all()
        return render_template("library.html", books=books, total=total, page=page, pages=pages,
                               q=q, status=status, statuses=BookStatus,
                               total_books=db().scalar(select(func.count()).select_from(Book)),
                               total_chapters=db().scalar(select(func.count()).select_from(Chapter)))

    @app.get("/books/<int:book_id>")
    def book_detail(book_id):
        book = get_book(book_id)
        total = db().scalar(select(func.count()).select_from(Chapter).where(Chapter.id_book == book_id))
        ready = db().scalar(select(func.count()).select_from(Chapter).where(
            Chapter.id_book == book_id, Chapter.file_path.is_not(None),
        ))
        page, pages, offset = pagination(total, app.config["CHAPTERS_PER_PAGE"])
        chapters = db().scalars(select(Chapter).where(Chapter.id_book == book_id).order_by(
            Chapter.number, Chapter.id_chapter).limit(app.config["CHAPTERS_PER_PAGE"]).offset(offset)).all()
        first = db().scalar(select(Chapter.id_chapter).where(Chapter.id_book == book_id).order_by(
            Chapter.number, Chapter.id_chapter).limit(1))
        return render_template("book.html", book=book, chapters=chapters, total=total,
                               ready=ready, first=first, page=page, pages=pages)

    @app.get("/chapters/<int:chapter_id>")
    def read_chapter(chapter_id):
        return chapter_view(get_chapter(chapter_id))

    @app.post("/chapters/<int:chapter_id>/load")
    def load_chapter(chapter_id):
        provided = request.form.get("csrf_token", "")
        if not provided or not secrets.compare_digest(provided.encode(), session.get("csrf_token", "").encode()):
            abort(400, description="This page expired. Reload the chapter page and try again.")
        with LOAD_LOCK:
            chapter = get_chapter(chapter_id)
            if cached_text(chapter.file_path, Path(app.config["DATA_ROOT"])):
                return redirect(url_for("read_chapter", chapter_id=chapter_id), code=303)
            loader = app.config["CHAPTER_LOADER"]
            if loader is None:
                if chapter.book.site.name != Novgo.name or urlsplit(chapter.url).hostname != urlsplit(Novgo.url).hostname:
                    return chapter_view(chapter, error="Chapter downloads are not supported for this source yet.", status=422)
                loader = lambda connection, item: Novgo(connection).process_chapter(item.url)
            try:
                loaded = loader(db(), chapter)
                if loaded and cached_text(chapter.file_path, Path(app.config["DATA_ROOT"])):
                    db().commit()
                    return redirect(url_for("read_chapter", chapter_id=chapter_id), code=303)
                db().rollback()
            except Exception as exc:
                db().rollback()
                logger.error("Cannot load chapter %d (%s)", chapter_id, type(exc).__name__)
            return chapter_view(get_chapter(chapter_id),
                                error="The chapter could not be loaded. Please try again in a moment.", status=502)

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        logger.error("Library database request failed (%s)", type(error).__name__)
        return render_template("error.html", code=503,
                               message="The library database is unavailable. Check its connection and try again."), 503

    for code in (400, 404, 503):
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
