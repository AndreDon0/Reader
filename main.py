from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from web import account, auth, library
from web.views import ROOT, render


app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.include_router(auth.router)
app.include_router(library.router)
app.include_router(account.router)


@app.get("/{page:path}")
def development(request: Request, page: str):
    title = {"catalog": "Catalog", "settings": "Settings"}.get(page, "Book" if page.startswith("books/") else "Page")
    return render(request, "development", status_code=200 if title != "Page" else 404, title=title)
