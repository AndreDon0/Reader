Project for learning how to read different languages.

It include site what download from other sites their new releases and display it in real time. After reader choosed their title for read, site download chapter and chapter after this from available sites and cache it.

Also it provide dictionary and helpfull AI assistant.

## Local reader website

Run the website alongside the existing worker:

```sh
uv sync
uv run python web.py
```

Open http://127.0.0.1:8000. Use `--port 8001` to choose another port.
The website reads the same `DATABASE_URL` from `.env`. It uses the existing
schema; it does not create or migrate the database.

Browse or search books by title/author, filter by status, open a book's chapter
list, and use previous/next navigation in the reader. Reading size, color theme,
and the last opened chapter for each book are remembered in your browser.

The database stores chapter metadata and a cache path. For a chapter that is
not downloaded, click **Load chapter**. NOVGO chapters are saved as text.
[Manhuazhan](https://www.manhuazhan.com/) chapters are saved as one `.cbz` file
each under `data/Manhuazhan/<book-id>/`. A CBZ is a ZIP of page images, numbered
in reading order. The browser displays the pages directly from that archive,
and cached chapters work without contacting the source. If a page download
fails, the reader offers a retry and no partial archive is saved.

The worker remains a separate process (`uv run python main.py`); opening the
website does not start a catalogue crawl. The worker imports recent releases
from the NOVGO and Manhuazhan homepages. Each process owns its own VPN pool.

VPN requests use `VPN_SUBSCRIPTION_URL` and the `xray` executable (or `XRAY_PATH`).
One Xray process keeps a separate local HTTP proxy route for each distinct
subscription server. Each route has a persistent HTTP session, allowing
connections to be reused across page requests and worker cycles.

At the start of each worker cycle, every route fetches the first configured
site's homepage through the VPN. Up to eight probes run at once, with a
five-second connect/read timeout each. The log reports how many routes work.
Requests rotate through those working routes. If a route fails during a request,
it is excluded until the next cycle's health check, which can restore it. Health
checks never use direct fallback. Standalone `request()` use also initializes and
checks the routes on first access.

If no working VPN routes remain, or VPN setup is unavailable, an error is logged
and the page request retries through the local connection with environment
proxies disabled. If that also fails, `request()` returns `None`. HTTP error
responses from page requests are returned without retrying them on other routes.
VPN mode does not support `stream=True` or caller-provided `proxies`.

Subscriptions refresh every five minutes. Unchanged subscriptions keep the same
process and sessions; changed subscriptions replace the routes. Refresh failures
retain cached routes and retry after 60 seconds. Dead Xray processes are restarted
and their routes checked before reuse. The worker closes sessions and stops Xray
on exit, including Ctrl-C and SIGTERM; standalone callers can explicitly call
`close_vpn_pools()`. Without a
subscription, requests work normally without starting Xray. Book and page
processing remains sequential.
