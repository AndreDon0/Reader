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
  const chapter = document.querySelector('[data-reader-book]');
  if (chapter) save(`reader.book.${chapter.dataset.readerBook}`, `/chapters/${chapter.dataset.readerChapter}`);
  document.querySelectorAll('[data-resume-book]').forEach(link => {
    const last = stored(`reader.book.${link.dataset.resumeBook}`);
    if (last && /^\/chapters\/\d+$/.test(last)) {
      link.href = last;
      link.textContent = 'Continue reading →';
    }
  });
  document.querySelector('[data-load-form]')?.addEventListener('submit', event => {
    const button = event.currentTarget.querySelector('[data-load-button]');
    button.disabled = true;
    button.textContent = 'Loading chapter…';
    event.currentTarget.setAttribute('aria-busy', 'true');
  });
})();
