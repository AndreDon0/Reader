"""Durable background work shared by the website and crawler."""

import logging
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import and_, cast, exists, func, literal, or_, select, true, update

from models import Book, CacheRemoval, Chapter, DownloadJob, Followed, FollowedStatus
from models import ImportJob, ImportRequest, JobState, User
from sites.registry import SITE_CLASSES
from utils.chapter_cache import cache_path, cached_pages, cached_text
from utils.db_write import upsert

logger = logging.getLogger(__name__)
SOURCES = {site.name: site for site in SITE_CLASSES}
ROOT = Path(__file__).resolve().parents[1]
LEASE = timedelta(minutes=15)
MAX_ATTEMPTS = 3


def is_cached(chapter, root):
    return bool(cached_text(chapter.file_path, root) or cached_pages(chapter.file_path, root))


def download_site(chapter):
    site = SOURCES.get(chapter.book.site.name)
    if site and site.supports_downloads:
        try:
            url = urlsplit(chapter.url)
            if url.scheme == "https" and url.netloc == urlsplit(site.url).netloc:
                return site
        except ValueError:
            pass
    return None


def enqueue_downloads(db, chapter_ids):
    job = upsert(db, DownloadJob).from_select(["id_chapter"], chapter_ids)
    db.execute(job.on_conflict_do_nothing(index_elements=["id_chapter"]))


def retry_download(db, chapter_id):
    job = upsert(db, DownloadJob).values(id_chapter=chapter_id)
    db.execute(job.on_conflict_do_update(
        index_elements=["id_chapter"], where=DownloadJob.state.in_((JobState.FAILED, JobState.READY)),
        set_={"state": JobState.QUEUED, "attempts": 0, "available_at": func.now()},
    ))


def add_to_planning(db, book_id, users):
    insert = upsert(db, Followed).from_select(
        ["id_user", "id_book", "status"],
        select(users.c.id_user, literal(book_id),
               cast(FollowedStatus.PLANNING.name, Followed.__table__.c.status.type)).where(true()),
    )
    db.execute(insert.on_conflict_do_nothing(index_elements=["id_user", "id_book"]))


def follow_import(db, source, url, user_id):
    insert = upsert(db, ImportJob).values(url=url, source=source)
    db.execute(insert.on_conflict_do_nothing(index_elements=["url"]))
    job = db.scalar(select(ImportJob).where(ImportJob.url == url).with_for_update())
    if job.state == JobState.READY:
        return job.id_book
    if job.state == JobState.FAILED:
        job.state = JobState.QUEUED
        job.attempts = 0
        job.available_at = datetime.now(UTC)
    insert = upsert(db, ImportRequest).values(url=url, id_user=user_id)
    db.execute(insert.on_conflict_do_nothing(index_elements=["url", "id_user"]))
    return None


def enqueue_watched(db, site_id=None):
    # Compare against the anchor chapter's current position, because a source can
    # renumber chapters when it fills an old gap.
    anchor = Chapter.__table__.alias("watch_anchor")
    watch = (select(Followed.id_book)
             .outerjoin(anchor, anchor.c.id_chapter == Followed.watch_after_id)
             .join(User, User.id_user == Followed.id_user)
             .where(Followed.id_book == Chapter.id_book, Followed.watch_since.is_not(None),
                    User.disabled.is_(False), Chapter.discovered_at >= Followed.watch_since,
                    or_(Followed.watch_after_id.is_(None),
                        and_(Chapter.id_chapter > Followed.watch_after_id,
                             Chapter.number > func.coalesce(anchor.c.number, Followed.watch_after_number))))
             .correlate(Chapter))
    ids = (select(Chapter.id_chapter)
           .join(Book, Book.id_book == Chapter.id_book)
           .where(Book.id_site == site_id, Book.hidden.is_(False), Chapter.hidden.is_(False),
                  Chapter.file_path.is_(None), exists(watch),
                  ~exists().where(DownloadJob.id_chapter == Chapter.id_chapter)))
    enqueue_downloads(db, ids)


def _claim(db, model, key, *filters):
    now = datetime.now(UTC)
    row = db.scalar(select(model).where(
        model.state.in_((JobState.QUEUED, JobState.RUNNING)), model.available_at <= now, *filters,
    ).order_by(model.available_at, key).limit(1).with_for_update(skip_locked=True))
    if row is None:
        return None
    row.state = JobState.RUNNING
    row.attempts += 1
    row.available_at = now + LEASE
    return getattr(row, key.key)


def _finish(job, success):
    if success:
        job.state = JobState.READY
    elif job.attempts >= MAX_ATTEMPTS:
        job.state = JobState.FAILED
    else:
        job.state = JobState.QUEUED
        job.available_at = datetime.now(UTC) + timedelta(seconds=30 * 4 ** (job.attempts - 1))


def run_import(factory, site_class):
    with factory.begin() as db:
        url = _claim(db, ImportJob, ImportJob.url, ImportJob.source == site_class.name)
    if url is None:
        return False
    with factory.begin() as db:
        book = db.scalar(select(Book).where(Book.url == url))
        if book is None:
            try:
                with db.begin_nested() as savepoint:
                    if not site_class(db).process_book(url):
                        savepoint.rollback()
                    else:
                        book = db.scalar(select(Book).where(Book.url == url))
            except Exception as exc:
                logger.error("Import failed on %s (%s)", site_class.name, type(exc).__name__)
        # This lock serializes finishing the job with users joining it. Source
        # requests run outside the lock so the website can accept new requests.
        job = db.scalar(select(ImportJob).where(ImportJob.url == url).with_for_update())
        if job is not None:
            if book is not None and not book.hidden:
                job.id_book = book.id_book
                users = (select(ImportRequest.id_user).join(User, User.id_user == ImportRequest.id_user)
                         .where(ImportRequest.url == url, User.disabled.is_(False)).subquery())
                add_to_planning(db, book.id_book, users)
                _finish(job, True)
            else:
                _finish(job, False)
    return True


def run_download(factory, root=ROOT):
    minimum = int(os.environ.get("READER_MIN_FREE_BYTES", str(10 * 1024 ** 3)))
    if shutil.disk_usage(root / "data").free < minimum:
        return False
    with factory.begin() as db:
        chapter_id = _claim(db, DownloadJob, DownloadJob.id_chapter)
    if chapter_id is None:
        return False
    with factory.begin() as db:
        # Acquire the chapter first, as does admin deletion. The file and its
        # database path are both stable until this transaction finishes.
        chapter = db.get(Chapter, chapter_id, with_for_update=True)
        job = db.get(DownloadJob, chapter_id, with_for_update=True)
        if chapter is None or job is None or job.state != JobState.RUNNING:
            return True
        site = download_site(chapter)
        if chapter.hidden or chapter.book.hidden or site is None:
            job.state = JobState.FAILED
            return True
        success = is_cached(chapter, root)
        if not success:
            try:
                with db.begin_nested() as savepoint:
                    success = site(db).process_chapter(chapter.url, data_root=root) and is_cached(chapter, root)
                    if not success:
                        savepoint.rollback()
            except Exception as exc:
                logger.error("Download failed for chapter %d (%s)", chapter_id, type(exc).__name__)
        _finish(job, success)
    return True


def remove_cached_file(factory, root=ROOT):
    with factory.begin() as db:
        item = db.scalar(select(CacheRemoval).order_by(CacheRemoval.path).limit(1).with_for_update(skip_locked=True))
        if item is None:
            return False
        for suffix in (".txt", ".cbz"):
            path = cache_path(item.path, root, suffix)
            if path is not None:
                path.unlink(missing_ok=True)
                break
        db.delete(item)
    return True


def recover_interrupted(factory):
    with factory.begin() as db:
        for model in (DownloadJob, ImportJob):
            db.execute(update(model).where(model.state == JobState.RUNNING)
                       .values(state=JobState.QUEUED, available_at=func.now()))


def run_cache_worker(factory, stop):
    while not stop.is_set():
        try:
            removed = remove_cached_file(factory)
        except Exception as exc:
            logger.warning("Could not remove a chapter cache file (%s). Retrying.", type(exc).__name__)
            removed = False
        try:
            downloaded = run_download(factory)
        except Exception:
            logger.exception("Download queue cycle failed")
            downloaded = False
        if not downloaded and not removed:
            stop.wait(3)
