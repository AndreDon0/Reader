import logging
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from utils.cbz import IMAGE_TYPES

logger = logging.getLogger(__name__)


def cache_path(file_path: str | None, root: Path, suffix: str) -> Path | None:
    """Resolve only chapter files within data/, even through symlinks."""
    if not file_path:
        return None
    try:
        path = (root / file_path).resolve()
        if path.suffix == suffix and path.is_relative_to((root / "data").resolve()):
            return path
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def cached_text(file_path: str | None, root: Path) -> str | None:
    path = cache_path(file_path, root, ".txt")
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None
    except (OSError, ValueError, UnicodeError) as exc:
        logger.warning("Cannot read cached chapter (%s)", type(exc).__name__)
        return None


def cached_pages(file_path: str | None, root: Path) -> list[str]:
    path = cache_path(file_path, root, ".cbz")
    if path is None:
        return []
    try:
        with ZipFile(path) as archive:
            return sorted(
                page.filename for page in archive.infolist()
                if page.file_size and "/" not in page.filename
                and Path(page.filename).suffix.lower() in IMAGE_TYPES
            )
    except (OSError, BadZipFile):
        return []


