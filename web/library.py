from calendar import month_abbr
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from database import SessionLocal
from models import Book, Chapter, Followed, FollowedStatus, ReadChapter
from web.auth import profile, same_origin, sign_in_required
from web.views import STATUSES, TZ, date_label, render


router = APIRouter()


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
    select(Chapter.id_book, Chapter.number, release_time.label("at"))
    .ext(distinct_on(Chapter.id_book))
    .order_by(Chapter.id_book, release_time.desc(), Chapter.number.desc())
    .subquery()
)


@router.get("/")
def index(request: Request):
    with SessionLocal() as session:
        user = profile(request, session)
        cards, read_at = book_cards(user.id_user if user else None)
        shelf = session.execute(
            cards.join(latest_release, latest_release.c.id_book == Book.id_book)
            .where(Followed.status != FollowedStatus.DROPPED)
            .where(read_at.is_(None) | (latest_release.c.at > read_at))
            .order_by(latest_release.c.at.desc(), Book.id_book)
            .limit(6)
        ).all() if user else []
        books = session.execute(
            cards.join(latest_release, latest_release.c.id_book == Book.id_book)
            .add_columns(latest_release.c.number, latest_release.c.at)
            .order_by(latest_release.c.at.desc(), Book.id_book)
            .limit(12)
        ).all()
    return render(request, "index", shelf=shelf, books=books, profile_enabled=user is not None)


@router.get("/search")
def search(request: Request, q: str = Query("", max_length=200)):
    q = q.strip()
    preview = request.headers.get("HX-Request") == "true"
    books = []
    user = None
    if q:
        with SessionLocal() as session:
            user = profile(request, session) if not preview else None
            cards, _ = book_cards(user.id_user if user else None)
            books = session.execute(
                cards.where(Book.title.icontains(q, autoescape=True))
                .order_by(Book.title, Book.id_book)
                .limit(10 if preview else 60)
            ).all()
    return render(request, "search_preview" if preview else "results", books=books, q=q,
                  profile_enabled=user is not None)


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
    with SessionLocal.begin() as session:
        user = profile(request, session, lock=True)
        if not user:
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
    if return_to.partition("?")[0] not in ("/", "/library", "/history", "/search"):
        return_to = "/library"
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
