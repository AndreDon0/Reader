(() => {
  const root = document.documentElement;
  const stored = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  const theme = stored('reader.theme');
  if (theme === 'dark' || theme === 'light') root.dataset.theme = theme;
  document.querySelector('[data-theme-toggle]')?.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    save('reader.theme', root.dataset.theme);
  });
  let size = Math.min(28, Math.max(16, Number(stored('reader.fontSize')) || 20));
  const applySize = () => root.style.setProperty('--reader-size', `${size}px`);
  applySize();
  document.querySelectorAll('[data-font]').forEach(button => button.addEventListener('click', () => {
    size = Math.min(28, Math.max(16, size + Number(button.dataset.font)));
    applySize();
    save('reader.fontSize', String(size));
  }));
  document.querySelectorAll('[data-cover]').forEach(image => {
    const hide = () => { image.hidden = true; };
    image.addEventListener('error', hide);
    if (image.complete && image.naturalWidth === 0) hide();
  });
  document.querySelector('[data-load-form]')?.addEventListener('submit', event => {
    const button = event.currentTarget.querySelector('[data-load-button]');
    button.disabled = true;
    button.textContent = 'Queuing chapter…';
    event.currentTarget.setAttribute('aria-busy', 'true');
  });
})();

(() => {
  const localUrls = new Set([...document.querySelectorAll('[data-local-source-url]')]
    .map(card => card.dataset.localSourceUrl));
  document.querySelectorAll('[data-source-search]').forEach(group => {
    async function load() {
      group.textContent = 'Searching…';
      try {
        const response = await fetch(group.dataset.sourceSearch);
        if (!response.ok || response.redirected) throw new Error('Search unavailable');
        group.innerHTML = await response.text();
        group.querySelectorAll('[data-result-url]').forEach(row => {
          if (localUrls.has(row.dataset.resultUrl)) row.remove();
        });
        const list = group.querySelector('.link-list');
        if (list && !list.children.length) group.textContent = 'Matches are already shown in Reader.';
      } catch {
        group.textContent = 'Source unavailable.';
      }
    }
    group.addEventListener('click', event => {
      if (event.target.closest('[data-retry-source]')) load();
    });
    load();
  });

  const download = document.querySelector('[data-download-status]');
  if (download) {
    const timer = setInterval(async () => {
      try {
        const response = await fetch(download.dataset.downloadStatus);
        if (!response.ok || response.redirected) return;
        const { state } = await response.json();
        if (state === 'ready' || state === 'failed') {
          clearInterval(timer);
          location.reload();
        } else if (state === 'running') {
          download.textContent = 'The worker is downloading this chapter.';
        }
      } catch {
        // Keep the manual refresh link available.
      }
    }, 5000);
  }
})();

(() => {
  const article = document.querySelector('[data-progress-url]');
  if (!article) return;
  const blocks = [...article.querySelectorAll('[data-reading-anchor]')];
  if (!blocks.length) return;

  const focusLine = () => Math.min(innerHeight * 0.2, 100);
  let anchor = Math.min(Number(article.dataset.anchor), blocks.length - 1);
  let offset = Number(article.dataset.offset);
  let finished = article.dataset.finished === '1';
  let sequence = 0;
  let last = '';
  let dirty = false;
  let restoring = anchor > 0 || offset > 0;
  const isComic = blocks[0].tagName === 'IMG';
  const scrollToAnchor = () => {
    const rect = blocks[anchor].getBoundingClientRect();
    window.scrollTo(0, scrollY + rect.top + offset * rect.height - focusLine());
  };

  function position() {
    const line = focusLine();
    const index = blocks.findIndex(block => block.getBoundingClientRect().bottom > line);
    anchor = index < 0 ? blocks.length - 1 : index;
    const rect = blocks[anchor].getBoundingClientRect();
    offset = Math.max(0, Math.min(1, (line - rect.top) / Math.max(1, rect.height)));
    if (dirty && blocks.at(-1).getBoundingClientRect().bottom <= innerHeight &&
        blocks.at(-1).getBoundingClientRect().bottom > 0) finished = true;
  }

  function save(ending = false) {
    if (dirty) position();
    const current = `${anchor}:${offset.toFixed(5)}:${finished}`;
    if (current === last && !ending) return;
    last = current;
    const data = new URLSearchParams({
      csrf_token: article.dataset.csrf, view: article.dataset.view,
      anchor, offset: offset.toFixed(5), sequence: ++sequence, finished: finished ? 1 : 0,
    });
    if (ending && navigator.sendBeacon(article.dataset.progressUrl, data)) return;
    fetch(article.dataset.progressUrl, { method: 'POST', body: data, keepalive: true })
      .then(response => { if (!response.ok && last === current) last = ''; })
      .catch(() => { if (last === current) last = ''; });
  }

  const resize = new ResizeObserver(() => {
    if (restoring) scrollToAnchor();
  });
  if (restoring) {
    resize.observe(article);
    if (isComic) blocks.slice(0, anchor + 1).forEach(image => { image.loading = 'eager'; });
    requestAnimationFrame(scrollToAnchor);
  }
  function startReading() {
    restoring = false;
    resize.disconnect();
    dirty = true;
  }
  ['wheel', 'touchmove', 'pointerdown', 'keydown'].forEach(event => {
    addEventListener(event, startReading, { once: true, passive: true });
  });
  addEventListener('scroll', () => { if (!restoring) dirty = true; }, { passive: true });
  save();
  setInterval(() => save(), 3000);
  addEventListener('pagehide', () => save(true));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') save(true);
  });
  document.querySelector('[data-next-chapter]')?.addEventListener('click', () => {
    finished = true;
    save(true);
  });
})();
