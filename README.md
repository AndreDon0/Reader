Project for learning how to read different languages.

It include site what download from other sites their new releases and display it in real time. After reader choosed their title for read, site download chapter and chapter after this from available sites and cache it.

Also it provide dictionary and helpfull AI assistant.

## Local reader website

Run the website alongside the existing worker:

```sh
uv sync
uv run alembic upgrade head
uv run python web.py
```

Open http://127.0.0.1:8000. Use `--port 8001` to choose another port.
The website reads the same `DATABASE_URL` from `.env`. It uses the existing
schema. Apply migrations before starting the web app or worker.

Register for a private library, choose languages and title types on Home, and use
the catalog search to find both local titles and matches from sources. **Add to
my library** imports a source result into Planning when the worker finishes.
Reading position is saved to your account for each chapter. Reading size and
color theme are saved in the browser.

The database stores chapter metadata and a cache path. For a chapter that is
not downloaded, click **Download chapter**, or use **Download title** and
**Auto-download new releases** on its title page. Keep the worker running to
process these jobs. NOVGO chapters are saved as text.
[Manhuazhan](https://www.manhuazhan.com/) chapters are saved as one `.cbz` file
each under `data/Manhuazhan/<book-id>/`. A CBZ is a ZIP of page images, numbered
in reading order. The browser displays the pages directly from that archive,
and cached chapters work without contacting the source. If a page download
fails, the reader offers a retry and no partial archive is saved.

The worker is a separate process (`uv run python main.py`). It runs one loop
per site in parallel, with a 120-second interval measured from each site's
cycle start. If a cycle overruns, that site finishes its current work before
starting again; other sites keep polling on their own schedules.

- NOVGO imports recent releases and gradually backfills the title URLs in its
  public sitemap (up to three missing books per cycle).
- Manhuazhan imports recent releases. Its public catalogue shows only the first
  50 pages, so the worker does not treat that listing as a full catalogue.
- [Senkuro](https://senkuro.me/) imports book metadata from its public manga
  catalogue, following cursors in batches of 100. It uses the server's direct
  connection even when a VPN subscription is configured. Senkuro entries link
  to the original site for reading.

Each completed catalogue batch is saved before the worker moves to the next
one. A restart starts the sweep from the beginning and reuses saved titles.

VPN requests use `VPN_SUBSCRIPTION_URL` and the `xray` executable (or `XRAY_PATH`).
One Xray process keeps a separate local HTTP proxy route for each distinct
subscription server. Each route has a persistent HTTP session, allowing
connections to be reused across page requests and worker cycles.

Every 120 seconds, the worker probes each route against the first VPN-enabled
site's homepage. Up to eight probes run at once, with a
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
subscription, requests work normally without starting Xray.

## Deploy to TrueNAS

Reader uses PostgreSQL 17, a Gunicorn web app, a catalogue and download worker,
and a Caddy HTTPS proxy. Only Caddy publishes a port, on the NAS's LAN IP.

1. In TrueNAS, enable Apps on the `storage` pool. Create the child datasets
   `storage/reader/postgres` and `storage/reader/chapters`.
2. Put a copy of the repository in `/mnt/storage/reader/app` on the NAS. Keep
   the Mac's `.env` and `.venv` on the Mac. From the NAS administrator shell,
   create a deployment environment file in the repository:

   ```sh
   cd /mnt/storage/reader/app
   umask 077
   (set -C; {
       printf 'READER_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)"
       printf 'READER_SECRET_KEY=%s\n' "$(openssl rand -hex 32)"
   } > .env.truenas)
   ```

   Keep this file across updates. Restrict the deployment directory and the
   environment file to the administrator. On a dataset with an inherited ACL,
   check effective access after setting the permissions. Use a hex database
   password so it can be used in the connection URL. Set `VPN_SUBSCRIPTION_URL`
   in this file if you use a VPN subscription. Set `READER_BIND_IP` or
   `READER_WEB_PORT` if the server address or website port differs from
   `192.168.88.125:8000`.
3. Put the PostgreSQL custom-format dump in a private location on the NAS and
   copy the **contents** of the Mac's `data/` directory into the `chapters`
   dataset. Give UID and GID 568 ownership of the chapter cache so the web and
   worker containers can write to it. Start PostgreSQL and restore into the
   fresh database. Replace the dump path in this example with its actual path:

   ```sh
   install -d -m 0700 /mnt/storage/reader/runtime /mnt/storage/reader/runtime/caddy
   install -d -m 0750 -o 568 -g 568 /mnt/storage/reader/runtime/logs
   chown -R 568:568 /mnt/storage/reader/chapters
   docker compose --env-file .env.truenas up -d --wait db
   docker compose --env-file .env.truenas exec -T db \
       pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
       -U reader -d reader < /private/path/to/reader.dump
   docker compose --env-file .env.truenas build web
   docker compose --env-file .env.truenas run --rm web alembic upgrade head
   docker compose --env-file .env.truenas run --rm web flask --app web:create_app create-admin admin
   ```

   `pg_restore` brings over the schema and the Alembic revision along with the
   data. The restore runs in one transaction, so an error rolls it back.
4. To manage Reader through TrueNAS Apps, stop the staging Compose project with
   `docker compose --env-file .env.truenas down`. The bind-mounted database and
   chapter files stay in their datasets. In **Apps → Discover Apps → Install via
   YAML**, name the app `reader` and paste this configuration:

   ```yaml
   include:
     - path: /mnt/storage/reader/app/compose.yaml
       env_file: /mnt/storage/reader/app/.env.truenas
   ```

   Open `https://192.168.88.125:8000` from the LAN and check a cached chapter.
   Copy only the public certificate from
   `/mnt/storage/reader/runtime/caddy/caddy/pki/authorities/local/root.crt`
   to each client and trust it for TLS. Keep the CA private key on the NAS.
   Check the worker logs for its first catalogue cycle. The services use
   `restart: unless-stopped` so Docker restarts them after a reboot.

For Git-based updates, keep a checkout at `/mnt/storage/reader/app` and keep the
NAS copy of `.env.truenas` there. After pushing a tested change from your Mac,
run these commands in the NAS administrator shell:

```sh
cd /mnt/storage/reader/app
git pull --ff-only &&
docker compose --env-file .env.truenas build web &&
midclt call -j app.redeploy reader
```

For releases with schema changes, build the image first, stop web and worker,
back up and snapshot the Reader datasets, and run Alembic with the new image
before redeploying. PostgreSQL, the chapter cache, and the private CA persist
across redeployments. Admin Settings shows recent worker output and NAS load.
