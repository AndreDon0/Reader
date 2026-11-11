from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from models import Book
from database import SessionLocal

app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    books = []

    if q.strip():
        with SessionLocal() as session:
            books = session.scalars(
                select(Book)
                .where(Book.title.ilike(f"%{q}%"))
                .limit(10)
            ).all()

    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={"books": books, "q": q}
    )
