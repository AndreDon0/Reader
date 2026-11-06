import enum
from datetime import date, datetime
from uuid import UUID, uuid4
from sqlalchemy import Column, ForeignKey, String, Date, Enum, Table, Index, Uuid, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SiteCheck(Base):
    __tablename__ = "site_check"

    id_site: Mapped[int] = mapped_column(
        ForeignKey("site.id_site", ondelete="CASCADE"),
        primary_key=True
    )
    site: Mapped["Site"] = relationship()

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True
    )
    is_up: Mapped[bool] = mapped_column()


class Site(Base):
    __tablename__ = "site"

    id_site: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    url: Mapped[str] = mapped_column(String(200), unique=True)

    books: Mapped[list["Book"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )


class Author(Base):
    __tablename__ = "author"

    id_author: Mapped[int] = mapped_column(primary_key=True)
    nickname: Mapped[str] = mapped_column(String(50), unique=True)

    books: Mapped[list["Book"]] = relationship(back_populates="author")


class BookStatus(enum.Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class BookType(enum.Enum):
    NOVEL = "novel"
    MANGA = "manga"
    MANHUA = "manhua"
    MANHWA = "manhwa"
    WESTERN = "western"
    UNKNOWN = "unknown"


book_tag = Table(
    "book_tag",
    Base.metadata,
    Column("id_book", ForeignKey("book.id_book"), primary_key=True),
    Column("id_tag", ForeignKey("tag.id_tag"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tag"

    id_tag: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)

    books: Mapped[list["Book"]] = relationship(
        secondary=book_tag,
        back_populates="tags",
    )

class BookAltTitle(Base):
    __tablename__ = "book_alt_title"

    id_alt_title: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column()

    id_book: Mapped[int] = mapped_column(ForeignKey("book.id_book"))
    book: Mapped["Book"] = relationship(back_populates="alt_titles")


class Book(Base):
    __tablename__ = "book"

    id_book: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column()
    description: Mapped[str] = mapped_column()
    rating: Mapped[float | None] = mapped_column()
    rated: Mapped[int | None] = mapped_column()
    rank: Mapped[int | None] = mapped_column()
    views: Mapped[int | None] = mapped_column()
    likes: Mapped[int | None] = mapped_column()
    bookmarked: Mapped[int | None] = mapped_column()
    chapters_count: Mapped[int] = mapped_column()
    published_on: Mapped[date | None] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(25))

    status: Mapped[BookStatus] = mapped_column(
        Enum(BookStatus, name="book_status"),
        default=BookStatus.UNKNOWN,
    )
    type: Mapped[BookType] = mapped_column(
        Enum(BookType, name="book_type"),
        default=BookType.UNKNOWN,
    )

    url: Mapped[str] = mapped_column(String(500), unique=True)
    cover_url: Mapped[str] = mapped_column(String(500))

    id_site: Mapped[int] = mapped_column(ForeignKey("site.id_site"))
    site: Mapped["Site"] = relationship(back_populates="books")

    id_author: Mapped[int | None] = mapped_column(
        ForeignKey("author.id_author", ondelete="SET NULL"),
    )
    author: Mapped["Author | None"] = relationship(
        back_populates="books",
    )

    tags: Mapped[list["Tag"]] = relationship(
        secondary=book_tag,
        back_populates="books",
    )

    alt_titles: Mapped[list["BookAltTitle"]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
    )

    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
        order_by="Chapter.number",
    )


class Chapter(Base):
    __tablename__ = "chapter"

    id_chapter: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    number: Mapped[int] = mapped_column()
    published_on: Mapped[datetime | None] = mapped_column(DateTime)
    url: Mapped[str] = mapped_column(String(500), unique=True)
    file_path: Mapped[str | None] = mapped_column(String(200), unique=True)

    id_book: Mapped[int] = mapped_column(ForeignKey("book.id_book"))
    book: Mapped["Book"] = relationship(back_populates="chapters")


class User(Base):
    __tablename__ = "users"

    id_user: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(String(50), unique=True)

    followed: Mapped[list["Followed"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    read_chapters: Mapped[list["ReadChapter"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class FollowedStatus(enum.Enum):
    READING = "reading"
    COMPLETED = "completed"
    DROPPED = "dropped"
    PLANNING = "planning"


class Followed(Base):
    __tablename__ = "followed"

    id_user: Mapped[UUID] = mapped_column(
        ForeignKey("users.id_user", ondelete="CASCADE"),
        primary_key=True,
    )
    user: Mapped["User"] = relationship(back_populates="followed")
    id_book: Mapped[int] = mapped_column(
        ForeignKey("book.id_book", ondelete="CASCADE"),
        primary_key=True,
    )
    book: Mapped["Book"] = relationship()
    status: Mapped[FollowedStatus] = mapped_column(
        Enum(FollowedStatus, name="followed_status"),
        default=FollowedStatus.READING,
    )


class ReadChapter(Base):
    __tablename__ = "readed"

    id_user: Mapped[UUID] = mapped_column(
        ForeignKey("users.id_user", ondelete="CASCADE"),
        primary_key=True,
    )
    user: Mapped["User"] = relationship(back_populates="read_chapters")
    id_chapter: Mapped[int] = mapped_column(
        ForeignKey("chapter.id_chapter", ondelete="CASCADE"),
        primary_key=True,
    )
    chapter: Mapped["Chapter"] = relationship()
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )



Index("ix_book_site", Book.id_site)
Index("ix_book_author", Book.id_author)

Index("ix_chapter_book_number", Chapter.id_book, Chapter.number)

Index("ix_book_alt_title_book", BookAltTitle.id_book)

Index("ix_book_tag_tag_book", book_tag.c.id_tag, book_tag.c.id_book)

Index("ix_followed_book", Followed.id_book)
Index("ix_read_chapter_chapter", ReadChapter.id_chapter)

Index("ix_site_check_site_time", SiteCheck.id_site, SiteCheck.checked_at)