import logging
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from config import FILES_PATH
from database import SessionLocal
from models import Book, Chapter
from sites.novgo import Novgo

logger = logging.getLogger(__name__)
LEGACY_CACHE = Path(__file__).resolve().parents[1] / "data"
CHAPTER_LOCK = 0x524452


def can_download(site):
    return urlsplit(site.url).hostname in {"novgo.net", "www.novgo.net"}


def chapter_location(chapter, site_id):
    return Path(str(site_id), str(chapter.id_book), str(chapter.id_chapter))


def cached_file(chapter, site_id):
    root = FILES_PATH.resolve()
    legacy = LEGACY_CACHE.resolve()
    candidates = [root / chapter_location(chapter, site_id)]
    if chapter.file_path:
        saved = Path(chapter.file_path)
        candidates.insert(0, root / saved)
        if saved.parts and saved.parts[0] == "data":
            candidates.insert(1, legacy.joinpath(*saved.parts[1:]))
    for candidate in candidates:
        path = candidate.resolve()
        if (path.is_relative_to(root) or path.is_relative_to(legacy)) and path.suffix.lower() in ("", ".txt", ".cbz"):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if S_ISREG(info.st_mode) and info.st_size:
                return path
    return None


def cache_chapter(session, chapter, site):
    if path := cached_file(chapter, site.id_site):
        return path
    if not can_download(site):
        return None
    session.execute(select(func.pg_advisory_xact_lock(CHAPTER_LOCK, chapter.id_chapter)))
    session.refresh(chapter, ["file_path", "name"])
    if path := cached_file(chapter, site.id_site):
        return path
    relative = chapter_location(chapter, site.id_site)
    root = FILES_PATH.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Chapter cache path is outside the storage directory")
    if name := Novgo.download_chapter(chapter.url, path):
        chapter.name = name
    chapter.file_path = str(relative)
    return path


def cache_book(book_id):
    try:
        with SessionLocal() as session:
            book = session.get(Book, book_id, options=[joinedload(Book.site)])
            if not book or not book.download_new or not can_download(book.site):
                return
            site = book.site
            chapters = session.scalars(
                select(Chapter).where(Chapter.id_book == book_id).order_by(Chapter.number)
            ).all()
        missing = [chapter.id_chapter for chapter in chapters if not cached_file(chapter, site.id_site)]
        if not missing:
            return
        lock_path = FILES_PATH / str(site.id_site) / str(book_id) / ".backfill.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                flock(lock, LOCK_EX | LOCK_NB)
            except BlockingIOError:
                return
            for chapter_id in missing:
                try:
                    with SessionLocal.begin() as session:
                        enabled = session.scalar(select(Book.download_new).where(Book.id_book == book_id))
                        if not enabled:
                            break
                        if chapter := session.get(Chapter, chapter_id):
                            cache_chapter(session, chapter, site)
                except Exception:
                    logger.exception("Could not cache chapter %s", chapter_id)
    except Exception:
        logger.exception("Could not backfill book %s", book_id)


def cache_enabled_books():
    with SessionLocal() as session:
        book_ids = session.scalars(select(Book.id_book).where(Book.download_new).order_by(Book.id_book)).all()
    for book_id in book_ids:
        cache_book(book_id)
