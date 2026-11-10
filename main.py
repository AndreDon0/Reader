
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Book
from database import SessionLocal

app = FastAPI()
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal.begin() as session:
        titles = session.scalars(
            select(Book.title).order_by(Book.title).limit(100)
        ).all()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"titles": titles},
    )
