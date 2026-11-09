from threading import Lock
from time import monotonic

from flask import abort, current_app, g, redirect, render_template, request, url_for
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from models import Author, Book, BookAltTitle, BookStatus, BookType, Chapter, Followed, FollowedStatus
from models import ImportJob, ImportRequest, ReadChapter, Site, Tag
from sites.base_site import SourceSearchUnavailable
from utils.jobs import SOURCES, follow_import
from utils.db_write import upsert

HOME_LIMIT = 8
RESULT_LIMIT = 12
PREFERENCES = {"english": "English", "chinese": "Chinese", "russian": "Russian"}


def language_options(db):
    languages = {site.language for site in SOURCES.values()}
    languages.update(db.scalars(select(Book.language).where(Book.hidden.is_(False)).distinct()))
    return {item: PREFERENCES.get(item, item.capitalize()) for item in sorted(languages) if item}


def parse_catalog(db):
    q = request.args.get("q", "").strip()[:200]
    broaden = request.args.get("all") == "1"
    available_languages = language_options(db)
    languages = set(request.args.getlist("language")) & available_languages.keys()
    types = set(request.args.getlist("type")) & {item.value for item in BookType}
    if not broaden:
        preferred = set(g.user.secondary_languages)
        if g.user.primary_language:
            preferred.add(g.user.primary_language)
        if preferred:
            languages = languages & preferred if languages else preferred
            if not languages:
                languages = {""}
        if g.user.book_types:
            preferred_types = set(g.user.book_types)
            types = types & preferred_types if types else preferred_types
            if not types:
                types = {""}
    source = request.args.get("source", "")
    source = source if source in SOURCES else ""
    status = request.args.get("status", "")
    status = status if status in {item.value for item in BookStatus} else ""
    tags = sorted(set(request.args.getlist("tag", type=int)))[:20]
    description = request.args.get("description") == "1"
    sort = request.args.get("sort", "")
    return q, languages, types, source, status, tags, description, sort


def book_filters(languages, types):
    filters = [Book.hidden.is_(False)]
    if languages:
        filters.append(Book.language.in_(languages))
    if types:
        filters.append(Book.type.in_([item for item in BookType if item.value in types]))
    return filters


def _pattern(q):
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def register(app, db, pagination):
    search_cache = {}
    search_lock = Lock()
    source_locks = {name: Lock() for name in SOURCES}

    @app.get("/")
    def library():
        user_id = g.user.id_user
        if request.args.get("q") or any(key in request.args for key in ("status", "page")):
            return redirect(url_for("catalog", **dict(request.args.lists())))
        languages = set(g.user.secondary_languages)
        if g.user.primary_language:
            languages.add(g.user.primary_language)

        release_date = func.coalesce(Chapter.published_on, Chapter.discovered_at)
        new_chapters = db().scalars(
            select(Chapter).join(Book).join(Followed, and_(Followed.id_book == Book.id_book, Followed.id_user == user_id))
            .outerjoin(ReadChapter, and_(ReadChapter.id_chapter == Chapter.id_chapter, ReadChapter.id_user == user_id))
            .options(joinedload(Chapter.book))
            .where(Chapter.hidden.is_(False), Book.hidden.is_(False), release_date >= Followed.followed_at,
                   Followed.status != FollowedStatus.DROPPED,
                   or_(ReadChapter.finished.is_(False), ReadChapter.finished.is_(None)))
            .order_by(release_date.desc(), Chapter.id_chapter.desc()).limit(HOME_LIMIT)
        ).all()
        history = (select(ReadChapter.id_chapter, ReadChapter.read_at, func.row_number().over(
            partition_by=Chapter.id_book, order_by=(ReadChapter.read_at.desc(), Chapter.id_chapter.desc()),
        ).label("position")).join(Chapter).join(Book)
            .where(ReadChapter.id_user == user_id, Chapter.hidden.is_(False), Book.hidden.is_(False))).subquery()
        last_read = db().scalars(select(Chapter).join(history, history.c.id_chapter == Chapter.id_chapter)
                                 .where(history.c.position == 1).options(joinedload(Chapter.book))
                                 .order_by(history.c.read_at.desc(), Chapter.id_chapter.desc()).limit(HOME_LIMIT)).all()
        updates = db().scalars(select(Book).where(*book_filters(languages, g.user.book_types),
                                                   Book.last_release_at.is_not(None))
                               .order_by(Book.last_release_at.desc(), Book.id_book.desc()).limit(HOME_LIMIT)).all()
        return render_template("home.html", languages=language_options(db()), types=BookType,
                               new_chapters=new_chapters, last_read=last_read, updates=updates)

    @app.post("/preferences")
    def preferences():
        options = language_options(db())
        primary = request.form.get("primary_language", "")
        if primary not in options:
            abort(400, description="Choose a primary language.")
        g.user.primary_language = primary
        g.user.secondary_languages = sorted(set(request.form.getlist("secondary")) & options.keys() - {primary})
        g.user.book_types = sorted(set(request.form.getlist("type")) & {item.value for item in BookType})
        db().commit()
        return redirect(url_for("library"), code=303)

    @app.get("/catalog")
    def catalog():
        q, languages, types, source, status, tags, description, sort = parse_catalog(db())
        filters = book_filters(languages, types)
        rank = None
        if q:
            pattern = _pattern(q)
            title = Book.title.ilike(pattern, escape="\\")
            alt = Book.alt_titles.any(BookAltTitle.title.ilike(pattern, escape="\\"))
            author = Book.author.has(Author.nickname.ilike(pattern, escape="\\"))
            matches = [title, alt, author]
            if description:
                matches.append(Book.description.ilike(pattern, escape="\\"))
            filters.append(or_(*matches))
            rank = case((func.lower(Book.title) == q.lower(), 0), (title, 1), (alt, 2), (author, 3), else_=4)
        if source:
            filters.append(Book.site.has(Site.name == source))
        if status:
            filters.append(Book.status == BookStatus(status))
        for tag in tags:
            filters.append(Book.tags.any(Tag.id_tag == tag))
        order = {
            "title": func.lower(Book.title),
            "updated": Book.last_release_at.desc().nulls_last(),
            "newest": Book.id_book.desc(),
        }.get(sort, rank if rank is not None else Book.id_book.desc())
        total = db().scalar(select(func.count()).select_from(Book).where(*filters))
        size = app.config["BOOKS_PER_PAGE"]
        page, pages, offset = pagination(total, size)
        books = db().scalars(select(Book).where(*filters).options(joinedload(Book.author), joinedload(Book.site))
                             .order_by(order, Book.id_book.desc()).offset(offset).limit(size)).all()
        remote = [site.name for site in SOURCES.values()
                  if q and (not source or site.name == source)
                  and (not languages or site.language in languages)
                  and (not types or ("" not in types and (site.search_type is None or site.search_type.value in types)))]
        arguments = dict(request.args.lists())
        arguments.pop("page", None)
        return render_template("library.html", books=books, total=total, page=page, pages=pages,
                               q=q, status=status, statuses=BookStatus, languages=language_options(db()),
                               types=BookType, sources=SOURCES, remote=remote, tags=db().scalars(
                                   select(Tag).order_by(Tag.name)).all(), args=arguments)

    @app.get("/search/sources/<provider>")
    def source_search(provider):
        source = provider
        site = SOURCES.get(source)
        if site is None:
            abort(404)
        q, languages, types, selected, *_ = parse_catalog(db())
        if (not q or (selected and selected != source) or (languages and site.language not in languages)
                or "" in types or (types and site.search_type and site.search_type.value not in types)):
            abort(404)
        # Don't keep a database transaction checked out while a remote site answers.
        db().commit()
        key = (source, q)
        with search_lock:
            entry = search_cache.get(key)
        if entry is None or entry[0] <= monotonic():
            if not source_locks[source].acquire(blocking=False):
                return render_template("source_results.html", source=source, busy=True)
            try:
                try:
                    found = site.search(q)
                except SourceSearchUnavailable:
                    found = None
                except Exception:
                    current_app.logger.warning("Search failed for %s", source, exc_info=False)
                    found = None
                entry = monotonic() + (20 if found is None else 180), found
                with search_lock:
                    if len(search_cache) >= 256:
                        search_cache.pop(next(iter(search_cache)))
                    search_cache[key] = entry
            finally:
                source_locks[source].release()
        if entry[1] is None:
            return render_template("source_results.html", source=source, unavailable=True)
        results = {}
        for hit in entry[1]:
            url = site.canonical_title_url(hit.url)
            if url and (not types or not hit.type or hit.type.value in types):
                results.setdefault(url, hit)
            if len(results) >= RESULT_LIMIT:
                break
        existing = {book.url: book for book in db().scalars(select(Book).where(Book.url.in_(results)))}
        items = [(url, hit.title, existing.get(url)) for url, hit in results.items()
                 if url not in existing or not existing[url].hidden]
        return render_template("source_results.html", source=source, results=items, site_url=site.url)

    @app.post("/imports")
    def request_import():
        site = SOURCES.get(request.form.get("source"))
        url = request.form.get("url", "")
        if site is None or not (url := site.canonical_title_url(url)):
            abort(400, description="Invalid source title.")
        book = db().scalar(select(Book).where(Book.url == url))
        if book and book.hidden:
            abort(404)
        book_id = book.id_book if book else follow_import(db(), site.name, url, g.user.id_user)
        if book_id is not None:
            insert = upsert(db(), Followed).values(id_user=g.user.id_user, id_book=book_id, status=FollowedStatus.PLANNING)
            db().execute(insert.on_conflict_do_nothing(index_elements=["id_user", "id_book"]))
        db().commit()
        return redirect(url_for("my_library"), code=303)

    @app.get("/my-library")
    def my_library():
        status = request.args.get("status", "")
        filters = [Followed.id_user == g.user.id_user, Book.hidden.is_(False)]
        if status in {item.value for item in FollowedStatus}:
            filters.append(Followed.status == FollowedStatus(status))
        size = app.config["BOOKS_PER_PAGE"]
        total = db().scalar(select(func.count()).select_from(Followed).join(Book).where(*filters))
        page, pages, offset = pagination(total, size)
        shelf = db().scalars(select(Followed).join(Book).where(*filters).options(joinedload(Followed.book))
                             .order_by(Followed.followed_at.desc(), Book.id_book.desc()).offset(offset).limit(size)).all()
        imports = db().scalars(select(ImportJob).join(ImportRequest)
                               .where(ImportRequest.id_user == g.user.id_user, ImportJob.id_book.is_(None))
                               .order_by(ImportJob.available_at.desc()).limit(20)).all()
        return render_template("shelf.html", shelf=shelf, statuses=FollowedStatus, imports=imports,
                               page=page, pages=pages, status=status)
