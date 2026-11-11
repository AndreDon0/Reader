import os
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates


ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo(os.getenv("READER_TIMEZONE", "Europe/Moscow"))
STATUSES = ("planning", "reading", "dropped", "completed")


templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals.update(statuses=STATUSES)


def date_label(day):
    return f"{day:%b} {day.day}, {day.year}"


templates.env.filters["read_date"] = lambda time: date_label(time.astimezone(TZ))


def render(request, name, status_code=200, **context):
    return templates.TemplateResponse(
        request, name + ".html", context, status_code=status_code, headers={"Cache-Control": "no-store"}
    )
