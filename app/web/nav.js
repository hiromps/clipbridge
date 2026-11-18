/* ClipBridge 共通ナビゲーション（PC 画面 / iPhone 画面）
 *
 * - <main> 内の [data-pane] を 1 つずつ表示する（縦に長いページを、サイドバーで切り替える画面にする）
 * - 広い画面ではサイドバーを常に表示し、狭い画面ではハンバーガーボタンでドロワーとして開く
 * - 表示中のページは URL の #ページ名 に残す（再読み込みしても同じページに戻る）。
 *   知らない # （#license=... など、ほかの用途）には触らない
 */
(() => {
  'use strict';

  const panes = Array.from(document.querySelectorAll('main [data-pane]'));
  const links = Array.from(document.querySelectorAll('[data-nav]'));
  const sidebar = document.getElementById('sidebar');
  const toggle = document.getElementById('navToggle');
  const scrim = document.getElementById('navScrim');
  if (!panes.length || !sidebar || !toggle) return;

  const names = panes.map((pane) => pane.dataset.pane);
  const fallback = names[0];
  const narrow = window.matchMedia('(max-width: 959px)');
  let current = null;

  function paneFromHash() {
    const name = decodeURIComponent(location.hash.slice(1));
    return names.includes(name) ? name : null;
  }

  function setOpen(open) {
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'メニューを閉じる' : 'メニューを開く');
    if (scrim) scrim.hidden = !open;
    // 狭い画面で閉じているドロワーは、Tab で中に入れないようにする
    sidebar.inert = narrow.matches && !open;
    if (open) {
      const active = sidebar.querySelector('[aria-current="page"]') || sidebar.querySelector('[data-nav]');
      if (active) active.focus({ preventScroll: true });
    }
  }

  function show(name, { focus = false, updateUrl = true } = {}) {
    if (!names.includes(name)) name = fallback;
    current = name;
    for (const pane of panes) pane.hidden = pane.dataset.pane !== name;
    for (const link of links) {
      const on = link.dataset.nav === name;
      if (on) {
        link.setAttribute('aria-current', 'page');
        link.classList.remove('has-news');
      } else {
        link.removeAttribute('aria-current');
      }
    }
    if (updateUrl && paneFromHash() !== name) history.replaceState(null, '', `${location.pathname}${location.search}#${name}`);
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (focus) {
      const pane = panes.find((p) => p.dataset.pane === name);
      const heading = pane && pane.querySelector('.pane-title');
      if (heading) heading.focus({ preventScroll: true });
    }
    document.dispatchEvent(new CustomEvent('clipnav:change', { detail: { pane: name } }));
  }

  for (const link of links) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      setOpen(false);
      show(link.dataset.nav, { focus: true });
    });
  }
  toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
  if (scrim) scrim.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('nav-open')) {
      setOpen(false);
      toggle.focus();
    }
  });
  window.addEventListener('hashchange', () => {
    const name = paneFromHash();
    if (name && name !== current) show(name, { updateUrl: false });
  });
  narrow.addEventListener('change', () => setOpen(false));

  // ページ内のボタンから別のページへ移るとき用（<a href="#pro" data-goto> など）
  document.addEventListener('click', (event) => {
    const goto = event.target.closest('[data-goto]');
    if (!goto) return;
    event.preventDefault();
    show(goto.dataset.goto, { focus: true });
  });

  window.ClipNav = {
    show: (name) => show(name, { focus: true }),
    get current() { return current; },
    /** 表示していないページに新着があることを、サイドバーの項目に点で示す */
    notify(name) {
      if (name === current) return;
      for (const link of links) if (link.dataset.nav === name) link.classList.add('has-news');
    },
    close: () => setOpen(false),
  };

  setOpen(false);
  show(paneFromHash() || fallback, { updateUrl: false });
})();
