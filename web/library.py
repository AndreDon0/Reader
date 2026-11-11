from calendar import month_abbr
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import AwareDatetime
from sqlalchemy import and_, case, func, select, true
from sqlalchemy.dialects.postgresql import insert

from database import SessionLocal
from models import Book, Chapter, Followed, FollowedStatus, ReadChapter, Site
from web.auth import profile, same_origin, sign_in_required
from web.views import STATUSES, TZ, date_label, render


router = APIRouter()
RELEASE_PAGE_SIZE = 12


def book_cards(user_id=None):
    last_read = (
        select(Chapter.id_book, func.max(ReadChapter.read_at).label("at"))
        .join(ReadChapter, ReadChapter.id_chapter == Chapter.id_chapter)
        .where(ReadChapter.id_user == user_id)
        .group_by(Chapter.id_book)
        .subquery()
    )
    cards = (
        select(Book, Followed.status, last_read.c.at)
        .outerjoin(Followed, and_(Followed.id_book == Book.id_book, Followed.id_user == user_id))
        .outerjoin(last_read, last_read.c.id_book == Book.id_book)
    )
    return cards, last_read.c.at


release_time = func.coalesce(Chapter.published_on, Chapter.discovered_at)
latest_release = (
    select(Chapter.number, release_time.label("at"))
    .where(Chapter.id_book == Book.id_book)
    .correlate(Book)
    .order_by(release_time.desc(), Chapter.number.desc())
    .limit(1)
    .lateral()
)


@router.get("/")
def index(request: Request, before: AwareDatetime | None = None,
          before_book: int | None = Query(None, ge=1, le=2_147_483_647)):
    if (before is None) != (before_book is None):
        raise HTTPException(400, "Provide both release cursor fields.")
    partial = before is not None and request.headers.get("HX-Request") == "true"
    with SessionLocal() as session:
        user = profile(request, session)
        cards, read_at = book_cards(user.id_user if user else None)
        releases = cards.join(latest_release, true())
        shelf = session.execute(
            releases
            .where(Followed.status != FollowedStatus.DROPPED)
            .where(read_at.is_(None) | (latest_release.c.at > read_at))
            .order_by(latest_release.c.at.desc(), Book.id_book)
            .limit(6)
        ).all() if user and not partial else []
        if before is not None:
            releases = releases.where(
                (latest_release.c.at < before)
                | ((latest_release.c.at == before) & (Book.id_book > before_book))
            )
        books = session.execute(
            releases.add_columns(latest_release.c.number, latest_release.c.at)
            .order_by(latest_release.c.at.desc(), Book.id_book)
            .limit(RELEASE_PAGE_SIZE + 1)
        ).all()
    next_page = None
    if len(books) > RELEASE_PAGE_SIZE:
        books = books[:RELEASE_PAGE_SIZE]
        book, _, _, _, released_at = books[-1]
        next_page = "/?" + urlencode({"before": released_at.isoformat(), "before_book": book.id_book})
    return render(request, "release_page" if partial else "index", shelf=shelf, books=books,
                  next_page=next_page, profile_enabled=user is not None)


@router.get("/search")
def search(request: Request, q: str = Query("", max_length=200)):
    q = q.strip()
    preview = request.headers.get("HX-Request") == "true"
    books = []
    user = None
    if q:
        relevance = case(
            (func.lower(Book.title) == func.lower(q), 0),
            (Book.title.istartswith(q, autoescape=True), 1),
            else_=2,
        )
        with SessionLocal() as session:
            user = profile(request, session) if not preview else None
            cards, _ = book_cards(user.id_user if user else None)
            books = session.execute(
                cards.where(Book.title.icontains(q, autoescape=True))
                .order_by(relevance, Book.title, Book.id_book)
                .limit(10 if preview else 60)
            ).all()
    return render(request, "search_preview" if preview else "results", books=books, q=q,
                  profile_enabled=user is not None)


@router.get("/catalog")
def catalog(request: Request, site: int = Query(0, ge=0, le=2_147_483_647),
            page: int = Query(1, ge=1, le=2_147_483_647)):
    page_size = 48
    with SessionLocal() as session:
        user = profile(request, session)
        cards, _ = book_cards(user.id_user if user else None)
        if site:
            cards = cards.where(Book.id_site == site)
        books = session.execute(
            cards.order_by(Book.title, Book.id_book).offset((page - 1) * page_size).limit(page_size + 1)
        ).all()
        sites = session.scalars(select(Site).order_by(Site.name)).all()
    return render(request, "catalog", books=books[:page_size], sites=sites, site=site, page=page,
                  has_next=len(books) > page_size, profile_enabled=user is not None)


@router.get("/library")
def library(request: Request, status: str = "all"):
    status = status if status in STATUSES else "all"
    with SessionLocal() as session:
        user = profile(request, session)
        if not user:
            return sign_in_required(request)
        cards, _ = book_cards(user.id_user)
        query = cards.where(Followed.status.is_not(None)).order_by(Book.title, Book.id_book)
        if status != "all":
            query = query.where(Followed.status == FollowedStatus(status))
        books = session.execute(query).all()
    return render(request, "library", books=books, selected=status, profile_enabled=True)


@router.post("/library/{book_id}")
def save_book(request: Request, book_id: int, status: FollowedStatus | Literal["remove"] = Form(),
              return_to: str = Form("/library")):
    if not same_origin(request):
        return render(request, "account", status_code=403, user=None)
    book_path = f"/books/{book_id}"
    return_path = return_to.partition("?")[0]
    if return_path not in ("/", "/library", "/history", "/search", "/catalog", book_path):
        return_to = "/library"
    with SessionLocal.begin() as session:
        user = profile(request, session, lock=True)
        if not user:
            if return_path == book_path:
                return sign_in_required(request, target=return_to)
            return RedirectResponse("/login?next=/library", status_code=303)
        if session.get(Book, book_id) is None:
            return render(request, "development", status_code=404, title="Book unavailable")
        if status == "remove":
            saved = session.get(Followed, (user.id_user, book_id))
            if saved:
                session.delete(saved)
        else:
            statement = insert(Followed).values(id_user=user.id_user, id_book=book_id, status=status)
            session.execute(statement.on_conflict_do_update(
                index_elements=[Followed.id_user, Followed.id_book], set_={"status": statement.excluded.status}
            ))
    return RedirectResponse(return_to, status_code=303)


@router.get("/history")
def history(request: Request, year: int | None = Query(None, ge=1970, le=9998)):
    today = datetime.now(TZ).date()
    year = min(year or today.year, today.year)
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    day = func.date(func.timezone(TZ.key, ReadChapter.read_at))
    with SessionLocal() as session:
        user = profile(request, session)
        if not user:
            return sign_in_required(request)
        cards, read_at = book_cards(user.id_user)
        counts = dict(session.execute(
            select(day, func.count()).where(
                ReadChapter.id_user == user.id_user,
                ReadChapter.read_at >= datetime(year, 1, 1, tzinfo=TZ),
                ReadChapter.read_at < datetime(year + 1, 1, 1, tzinfo=TZ),
            ).group_by(day)
        ).tuples().all())
        books = session.execute(
            cards.where(read_at.is_not(None))
            .order_by(read_at.desc(), Book.id_book)
            .limit(24)
        ).all()
    first = start - timedelta(days=start.weekday())
    weeks = [
        [first + timedelta(days=offset + weekday) for weekday in range(7)]
        for offset in range(0, (end - first).days, 7)
    ]
    months = [(month_abbr[month], (date(year, month, 1) - first).days // 7) for month in range(1, 13)]
    selected = max((day for day in counts if day <= today), default=min(end - timedelta(days=1), today))
    return render(request, "history", books=books, year=year, today=today, weeks=weeks,
                  months=months, counts=counts, selected=selected, date_label=date_label, profile_enabled=True)
