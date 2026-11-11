import logging
import re
from math import isfinite
from typing import Literal
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from database import SessionLocal
from models import Book, Chapter, ReadChapter
from sites.downloads import cache_book, cache_chapter, cached_file, can_download
from utils.cbz import IMAGE_TYPES, image_suffix
from web.auth import profile, same_origin, sign_in_required
from web.views import render

router = APIRouter()
logger = logging.getLogger(__name__)


def get_book(session, book_id, *, lock=False):
    book = session.get(Book, book_id, with_for_update=lock)
    if not book:
        raise HTTPException(404, "Book unavailable")
    return book


def get_chapter(session, book_id, chapter_id):
    chapter = session.scalar(
        select(Chapter).options(joinedload(Chapter.book).joinedload(Book.site))
        .where(Chapter.id_book == book_id, Chapter.id_chapter == chapter_id)
    )
    if not chapter:
        raise HTTPException(404, "Chapter unavailable")
    return chapter


def contents(session, book_id, order="asc"):
    number = Chapter.number.desc() if order == "desc" else Chapter.number
    return session.scalars(select(Chapter).where(Chapter.id_book == book_id).order_by(number)).all()


def archive_pages(archive):
    return sorted(
        (member for member in archive.infolist()
         if not member.is_dir() and any(member.filename.lower().endswith(ext) for ext in IMAGE_TYPES)),
        key=lambda member: [int(part) if index % 2 else part.lower()
                            for index, part in enumerate(re.split(r"(\d+)", member.filename))],
    )


@router.get("/books/{book_id}")
def book_page(request: Request, book_id: int, order: Literal["asc", "desc"] = "asc", failed: bool = False):
    with SessionLocal() as session:
        book = get_book(session, book_id)
        site = book.site
        chapters = contents(session, book_id, order)
        downloaded = {chapter.id_chapter for chapter in chapters if cached_file(chapter, site.id_site)}
        signed_in = profile(request, session) is not None
    return render(request, "book", book=book, site=site, chapters=chapters, order=order,
                  downloaded=downloaded, can_download=can_download(site), signed_in=signed_in, failed=failed)


@router.post("/books/{book_id}/download")
def book_download(request: Request, background: BackgroundTasks, book_id: int,
                  enabled: bool = Form(), order: Literal["asc", "desc"] = Form("asc")):
    if not same_origin(request):
        raise HTTPException(403)
    with SessionLocal.begin() as session:
        if not profile(request, session):
            return sign_in_required(request, target=f"/books/{book_id}")
        book = get_book(session, book_id, lock=True)
        if enabled and not can_download(book.site):
            raise HTTPException(409, "Downloads are unavailable for this source")
        book.download_new = enabled
    if enabled:
        background.add_task(cache_book, book_id)
    return RedirectResponse(f"/books/{book_id}?order={order}", status_code=303)


@router.post("/books/{book_id}/chapters/{chapter_id}/download")
def chapter_download(request: Request, book_id: int, chapter_id: int,
                     order: Literal["asc", "desc"] = Form("asc")):
    if not same_origin(request):
        raise HTTPException(403)
    failed = False
    try:
        with SessionLocal.begin() as session:
            if not profile(request, session):
                return sign_in_required(request, target=f"/books/{book_id}")
            chapter = get_chapter(session, book_id, chapter_id)
            failed = cache_chapter(session, chapter, chapter.book.site) is None
    except (OSError, ValueError):
        logger.exception("Could not cache chapter %s", chapter_id)
        failed = True
    return RedirectResponse(f"/books/{book_id}?order={order}&failed={int(failed)}#chapter-{chapter_id}", status_code=303)


@router.get("/books/{book_id}/chapters/{chapter_id}")
def chapter_page(request: Request, book_id: int, chapter_id: int):
    with SessionLocal() as session:
        chapter = get_chapter(session, book_id, chapter_id)
        book = chapter.book
        site = book.site
        chapters = contents(session, book_id)
        index = next((i for i, entry in enumerate(chapters) if entry.id_chapter == chapter_id), None)
        if index is None:
            raise HTTPException(404, "Chapter unavailable")
        previous = chapters[index - 1] if index else None
        following = chapters[index + 1] if index + 1 < len(chapters) else None
        content, page_count, progress = "", 0, None
        try:
            path = cache_chapter(session, chapter, site)
            if path and path.suffix.lower() == ".cbz":
                with ZipFile(path) as archive:
                    page_count = len(archive_pages(archive))
            elif path:
                content = path.read_text(encoding="utf-8-sig").strip()
                heading, separator, body = content.partition("\n\n")
                if separator and heading == chapter.name:
                    content = body
            if (content or page_count) and (user := profile(request, session)):
                seen = insert(ReadChapter).values(id_user=user.id_user, id_chapter=chapter_id)
                stopped_at = session.scalar(seen.on_conflict_do_update(
                    index_elements=[ReadChapter.id_user, ReadChapter.id_chapter], set_={"read_at": func.now()}
                ).returning(ReadChapter.stopped_at))
                progress = max(0, min(stopped_at, 1)) if isfinite(stopped_at) else 0
            session.commit()
        except (BadZipFile, OSError, ValueError):
            session.rollback()
            logger.exception("Could not open chapter %s", chapter_id)
        return render(request, "chapter", book=book, site=site, chapter=chapter, content=content,
                      page_count=page_count, chapters=chapters, previous=previous, following=following,
                      progress=progress)


@router.post("/books/{book_id}/chapters/{chapter_id}/progress", status_code=204)
def chapter_progress(request: Request, book_id: int, chapter_id: int, stopped_at: float = Form(ge=0, le=1)):
    if not same_origin(request):
        raise HTTPException(403)
    with SessionLocal.begin() as session:
        user = profile(request, session)
        if not user:
            raise HTTPException(401)
        matching_chapter = select(Chapter.id_chapter).where(
            Chapter.id_book == book_id, Chapter.id_chapter == chapter_id
        ).exists()
        result = session.execute(
            update(ReadChapter).where(
                ReadChapter.id_user == user.id_user, ReadChapter.id_chapter == chapter_id, matching_chapter,
            ).values(stopped_at=stopped_at, read_at=ReadChapter.read_at)
        )
        if not result.rowcount:
            raise HTTPException(404, "Reading record unavailable")
    return Response(status_code=204)


@router.get("/books/{book_id}/chapters/{chapter_id}/pages/{page}")
def chapter_image(book_id: int, chapter_id: int, page: int):
    with SessionLocal() as session:
        chapter = get_chapter(session, book_id, chapter_id)
        path = cached_file(chapter, chapter.book.id_site)
    if path and path.suffix.lower() == ".cbz":
        try:
            with ZipFile(path) as archive:
                pages = archive_pages(archive)
                if 1 <= page <= len(pages):
                    data = archive.read(pages[page - 1])
                    if suffix := image_suffix(data):
                        return Response(data, media_type=IMAGE_TYPES[suffix], headers={"X-Content-Type-Options": "nosniff"})
        except (BadZipFile, OSError, RuntimeError):
            logger.exception("Could not open image %s of chapter %s", page, chapter_id)
    raise HTTPException(404, "Page unavailable")
