import os
import shutil
from pathlib import Path

from flask import abort, g, redirect, render_template, request, url_for
from sqlalchemy import delete, func, select
from sqlalchemy.orm import joinedload, selectinload

from models import Author, Book, BookAltTitle, BookStatus, BookType, CacheRemoval, Chapter, DownloadJob
from models import ImportJob, Site, SiteCheck, Tag, User
from reader_web.catalog import language_options
from utils.db_write import upsert


def _tail_log():
    path = Path(os.environ.get("READER_WORKER_LOG", "logs/worker.log"))
    try:
        with path.open("rb") as file:
            file.seek(max(0, path.stat().st_size - 128 * 1024))
            return "\n".join(file.read(128 * 1024).decode("utf-8", errors="replace").splitlines()[-200:])
    except OSError:
        return ""


def _host_usage(root):
    usage = {}
    try:
        usage["disk"] = shutil.disk_usage(root / "data")
    except OSError:
        pass
    try:
        usage["load"] = Path(os.environ.get("READER_LOADAVG", "/proc/loadavg")).read_text().split()[:3]
        meminfo = Path(os.environ.get("READER_MEMINFO", "/proc/meminfo")).read_text().splitlines()
        memory = {key: int(value.split()[0]) * 1024 for key, value in
                  (line.split(":", 1) for line in meminfo if ":" in line)}
        usage["memory"] = memory["MemTotal"] - memory["MemAvailable"], memory["MemTotal"]
    except (OSError, KeyError, ValueError):
        pass
    return usage


def _named(db, model, field, values):
    if not values:
        return []
    column = getattr(model, field)
    insert = upsert(db, model).values([{field: value} for value in values])
    db.execute(insert.on_conflict_do_nothing(index_elements=[field]))
    return list(db.scalars(select(model).where(column.in_(values))))


def _lines(text, maximum):
    items = list(dict.fromkeys(item.strip() for item in text.splitlines() if item.strip()))
    if len(items) > 100 or any(len(item) > maximum for item in items):
        abort(400, description="Too many entries or an entry is too long.")
    return items


def register(app, db, pagination, safe_url):
    @app.before_request
    def check_admin():
        if request.endpoint and request.endpoint.startswith("admin_") and g.user and not g.user.is_admin:
            abort(403)

    @app.get("/settings/admin")
    def admin_settings():
        sites = db().scalars(select(Site).order_by(Site.name)).all()
        checks = [(site, db().scalar(select(SiteCheck).where(SiteCheck.id_site == site.id_site)
                                     .order_by(SiteCheck.checked_at.desc()).limit(1))) for site in sites]
        job_counts = [(label, db().execute(select(model.state, func.count()).group_by(model.state)).all())
                      for label, model in (("Downloads", DownloadJob), ("Imports", ImportJob))]
        users = db().scalars(select(User).order_by(User.username)).all()
        return render_template("admin.html", users=users, checks=checks, jobs=job_counts,
                               logs=_tail_log(), usage=_host_usage(Path(app.config["DATA_ROOT"])))

    @app.get("/settings/admin/catalog")
    def admin_catalog():
        q = request.args.get("q", "").strip()[:200]
        filters = []
        if q:
            filters.append(func.lower(Book.title).contains(q.lower(), autoescape=True))
        if request.args.get("hidden") == "1":
            filters.append(Book.hidden.is_(True))
        page, pages, offset = pagination(db().scalar(select(func.count()).select_from(Book).where(*filters)), 50)
        books = db().scalars(select(Book).where(*filters).order_by(Book.id_book.desc()).offset(offset).limit(50))
        return render_template("admin_catalog.html", books=books, page=page, pages=pages, q=q)

    @app.post("/settings/admin/users/<uuid:user_id>")
    def admin_user(user_id):
        # Serialize changes to active admins so two concurrent edits cannot
        # remove the last one.
        active = db().scalars(select(User).where(User.is_admin.is_(True), User.disabled.is_(False))
                              .order_by(User.id_user).with_for_update()).all()
        user = db().get(User, user_id, with_for_update=True)
        if user is None:
            abort(404)
        action = request.form.get("action")
        is_admin = request.form.get("is_admin") == "1"
        disabled = request.form.get("disabled") == "1"
        if user in active and len(active) == 1 and (action == "delete" or not is_admin or disabled):
            abort(400, description="Keep at least one active administrator.")
        if action == "delete":
            require_delete_confirmation()
            db().delete(user)
        elif action == "save":
            if (user.is_admin, user.disabled) != (is_admin, disabled):
                user.is_admin, user.disabled = is_admin, disabled
                user.session_version += 1
        else:
            abort(400)
        db().commit()
        return redirect(url_for("admin_settings"), code=303)

    @app.route("/settings/admin/books/<int:book_id>", methods=["GET", "POST"])
    def admin_book(book_id):
        query = select(Book).where(Book.id_book == book_id).options(
            selectinload(Book.author), selectinload(Book.tags), selectinload(Book.alt_titles),
        )
        if request.method == "POST":
            query = query.with_for_update()
        book = db().scalar(query)
        if book is None:
            abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "delete":
                require_delete_confirmation()
                remove_chapters(Chapter.id_book == book_id)
                db().execute(delete(Book).where(Book.id_book == book_id))
                db().commit()
                return redirect(url_for("admin_catalog"), code=303)
            if action != "save":
                abort(400)
            update_book(book)
            db().commit()
            return redirect(url_for("admin_book", book_id=book_id), code=303)
        page, pages, offset = pagination(db().scalar(select(func.count()).select_from(Chapter)
                                                    .where(Chapter.id_book == book_id)), 50)
        chapters = db().scalars(select(Chapter).where(Chapter.id_book == book_id)
                               .order_by(Chapter.number, Chapter.id_chapter).offset(offset).limit(50)).all()
        return render_template("admin_book.html", book=book, chapters=chapters, page=page, pages=pages,
                               statuses=BookStatus, types=BookType, languages=language_options(db()))

    def update_book(book):
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        cover = request.form.get("cover_url", "").strip()
        author = request.form.get("author", "").strip()
        language = request.form.get("language", "")
        try:
            book_type = BookType(request.form.get("type"))
            status = BookStatus(request.form.get("status"))
        except ValueError:
            abort(400, description="Invalid title type or status.")
        if (not title or len(title) > 1000 or len(cover) > 500 or (cover and not safe_url(cover))
                or len(author) > 50 or language not in language_options(db()) | {book.language: book.language}):
            abort(400, description="Invalid title, cover, author, or language.")
        values = {
            "title": title, "description": description, "cover_url": cover,
            "language": language, "type": book_type, "status": status,
        }
        edited = set(book.edited_fields)
        for field, value in values.items():
            if getattr(book, field) != value:
                setattr(book, field, value)
                edited.add(field)
        if author != (book.author.nickname if book.author else ""):
            book.author = next(iter(_named(db(), Author, "nickname", [author] if author else [])), None)
            edited.add("author")
        tags = _lines(request.form.get("tags", ""), 50)
        if set(tags) != {tag.name for tag in book.tags}:
            book.tags = _named(db(), Tag, "name", tags)
            edited.add("tags")
        titles = _lines(request.form.get("alt_titles", ""), 1000)
        old_titles = {item.title: item for item in book.alt_titles}
        if set(titles) != old_titles.keys():
            book.alt_titles = [old_titles.get(title) or BookAltTitle(title=title) for title in titles]
            edited.add("alt_titles")
        book.edited_fields = sorted(edited)
        book.hidden = request.form.get("hidden") == "1"

    @app.route("/settings/admin/chapters/<int:chapter_id>", methods=["GET", "POST"])
    def admin_chapter(chapter_id):
        chapter = db().get(Chapter, chapter_id, with_for_update=request.method == "POST")
        if chapter is None:
            abort(404)
        if request.method == "POST":
            book_id = chapter.id_book
            action = request.form.get("action")
            if action == "delete":
                require_delete_confirmation()
                remove_chapters(Chapter.id_chapter == chapter_id)
            elif action == "save":
                name = request.form.get("display_name", "").strip()
                if len(name) > 1000:
                    abort(400, description="Chapter title is too long.")
                chapter.display_name = name or None
                chapter.hidden = request.form.get("hidden") == "1"
            else:
                abort(400)
            db().commit()
            return redirect(url_for("admin_book", book_id=book_id), code=303)
        return render_template("admin_chapter.html", chapter=chapter)

    def require_delete_confirmation():
        if request.form.get("confirm") != "DELETE":
            abort(400, description="Type DELETE to confirm permanent deletion.")

    def remove_chapters(condition):
        db().execute(select(Chapter.id_chapter).where(condition).order_by(Chapter.id_chapter).with_for_update()).all()
        paths = select(Chapter.file_path).where(condition, Chapter.file_path.is_not(None))
        insert = upsert(db(), CacheRemoval).from_select(["path"], paths)
        db().execute(insert.on_conflict_do_nothing(index_elements=["path"]))
        db().execute(delete(Chapter).where(condition))
