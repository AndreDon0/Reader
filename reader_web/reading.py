import time
from datetime import UTC, datetime
from pathlib import Path

from flask import abort, current_app, g, redirect, request, url_for
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import and_, func, or_, select

from models import Chapter, DownloadJob, Followed, FollowedStatus, ReadChapter
from utils.db_write import upsert
from utils.jobs import SOURCES, download_site, enqueue_downloads, is_cached, retry_download


def _view_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="reader-progress")


def progress_view(chapter, count):
    if not count:
        return None
    return _view_signer().dumps([str(g.user.id_user), chapter.id_chapter, count, time.time_ns()])


def register(app, db, get_book, get_chapter):
    @app.post("/books/<int:book_id>/follow")
    def follow_book(book_id):
        book = get_book(book_id)
        if book.hidden:
            abort(404)
        status = request.form.get("status", "")
        if not status:
            follow = db().get(Followed, (g.user.id_user, book_id))
            if follow is not None:
                db().delete(follow)
        elif status in {item.value for item in FollowedStatus}:
            insert = upsert(db(), Followed).values(
                id_user=g.user.id_user, id_book=book_id, status=FollowedStatus(status),
            )
            db().execute(insert.on_conflict_do_update(
                index_elements=["id_user", "id_book"], set_={"status": FollowedStatus(status)},
            ))
        else:
            abort(400, description="Choose a library status.")
        db().commit()
        return redirect(url_for("book_detail", book_id=book_id), code=303)

    @app.post("/books/<int:book_id>/watch")
    def watch_book(book_id):
        book = get_book(book_id)
        site = SOURCES.get(book.site.name)
        if book.hidden or not site or not site.supports_downloads:
            abort(422)
        enabled = request.form.get("enabled") == "1"
        if enabled:
            insert = upsert(db(), Followed).values(
                id_user=g.user.id_user, id_book=book_id, status=FollowedStatus.PLANNING,
            )
            db().execute(insert.on_conflict_do_nothing(index_elements=["id_user", "id_book"]))
        follow = db().get(Followed, (g.user.id_user, book_id), with_for_update=True)
        if enabled and follow and follow.watch_since is None:
            anchor = db().scalar(select(Chapter).where(Chapter.id_book == book_id)
                                 .order_by(Chapter.number.desc(), Chapter.id_chapter.desc()).limit(1))
            follow.watch_since = datetime.now(UTC)
            follow.watch_after_id = anchor.id_chapter if anchor else None
            follow.watch_after_number = anchor.number if anchor else None
        elif not enabled and follow is not None:
            follow.watch_since = None
            follow.watch_after_id = None
            follow.watch_after_number = None
        db().commit()
        return redirect(url_for("book_detail", book_id=book_id), code=303)

    @app.post("/books/<int:book_id>/download")
    def download_book(book_id):
        book = get_book(book_id)
        site = SOURCES.get(book.site.name)
        if book.hidden or not site or not site.supports_downloads:
            abort(422)
        enqueue_downloads(db(), select(Chapter.id_chapter).where(
            Chapter.id_book == book_id, Chapter.hidden.is_(False), Chapter.file_path.is_(None),
        ))
        db().commit()
        return redirect(url_for("book_detail", book_id=book_id), code=303)

    @app.post("/chapters/<int:chapter_id>/load")
    def load_chapter(chapter_id):
        chapter = get_chapter(chapter_id)
        if chapter.hidden or chapter.book.hidden:
            abort(404)
        if not is_cached(chapter, Path(app.config["DATA_ROOT"])):
            if not download_site(chapter):
                abort(422, description="Chapter downloads are not supported for this source.")
            retry_download(db(), chapter_id)
            db().commit()
        return redirect(url_for("read_chapter", chapter_id=chapter_id), code=303)

    @app.get("/chapters/<int:chapter_id>/download-status")
    def download_status(chapter_id):
        chapter = get_chapter(chapter_id)
        if chapter.hidden or chapter.book.hidden:
            abort(404)
        if is_cached(chapter, Path(app.config["DATA_ROOT"])):
            return {"state": "ready"}
        job = db().get(DownloadJob, chapter_id)
        return {"state": job.state if job else "missing"}

    @app.post("/chapters/<int:chapter_id>/progress")
    def save_progress(chapter_id):
        chapter = get_chapter(chapter_id)
        if chapter.hidden or chapter.book.hidden:
            abort(404)
        try:
            user, chapter_key, count, stamp = _view_signer().loads(request.form.get("view", ""), max_age=7 * 86400)
            anchor = int(request.form.get("anchor", ""))
            offset = float(request.form.get("offset", ""))
            sequence = int(request.form.get("sequence", ""))
        except (BadData, TypeError, ValueError):
            abort(400, description="Invalid reading position.")
        if (user != str(g.user.id_user) or chapter_key != chapter_id
                or not 0 <= anchor < count or not 0 <= offset <= 1 or not 0 < sequence < 2 ** 31):
            abort(400, description="Invalid reading position.")
        insert = upsert(db(), ReadChapter).values(
            id_user=g.user.id_user, id_chapter=chapter_id, anchor=anchor, anchor_offset=offset,
            view_stamp=stamp, save_sequence=sequence, finished=request.form.get("finished") == "1",
            read_at=datetime.now(UTC),
        )
        new = insert.excluded
        db().execute(insert.on_conflict_do_update(
            index_elements=["id_user", "id_chapter"],
            where=or_(ReadChapter.view_stamp < new.view_stamp,
                      and_(ReadChapter.view_stamp == new.view_stamp, ReadChapter.save_sequence < new.save_sequence)),
            set_={
                "anchor": new.anchor, "anchor_offset": new.anchor_offset, "view_stamp": new.view_stamp,
                "save_sequence": new.save_sequence, "read_at": new.read_at,
                "finished": or_(ReadChapter.finished, new.finished),
            },
        ))
        db().commit()
        return "", 204
