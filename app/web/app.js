/* ClipBridge iPhone 側クライアント */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = 'clipbridge.token';
  const PC_NAME_KEY = 'clipbridge.pcName';

  const els = {
    conn: $('conn'),
    pairView: $('pairView'),
    mainView: $('mainView'),
    pairForm: $('pairForm'),
    codeInput: $('codeInput'),
    pairButton: $('pairButton'),
    pairMsg: $('pairMsg'),
    clipText: $('clipText'),
    clipMeta: $('clipMeta'),
    newBadge: $('newBadge'),
    copyBtn: $('copyBtn'),
    copyFallback: $('copyFallback'),
    copyHint: $('copyHint'),
    sendBtn: $('sendBtn'),
    sendText: $('sendText'),
    sendMsg: $('sendMsg'),
    a2hs: $('a2hs'),
    pcName: $('pcName'),
    unpairBtn: $('unpairBtn'),
    toast: $('toast'),
  };

  // ---- 保存領域（ホーム画面版でも使えるよう例外を握りつぶす） ------------
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  // ---- URL からの引き継ぎ（QR / manifest start_url / 失敗時のメッセージ） --
  const url = new URL(location.href);
  const tokenFromUrl = url.searchParams.get('t');
  const pairResult = url.searchParams.get('pair');
  if (tokenFromUrl) storage.set(TOKEN_KEY, tokenFromUrl);
  if (tokenFromUrl || pairResult) history.replaceState(null, '', '/');

  let token = storage.get(TOKEN_KEY);
  let current = null;        // 最新の PC クリップボード
  let lastHash = null;
  let eventSource = null;
  let reconnectTimer = null;
  let toastTimer = null;
  let sendTimer = null;
  let firstEvent = true;

  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;

  // ---- 表示ユーティリティ -------------------------------------------------
  function toast(text, kind = '') {
    els.toast.textContent = text;
    els.toast.className = 'toast ' + kind;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 2600);
  }

  function setConn(state, label) {
    els.conn.className = 'pill ' + state;
    els.conn.textContent = label;
  }

  function relativeTime(epochSeconds) {
    const diff = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (diff < 5) return 'たった今';
    if (diff < 60) return `${Math.floor(diff)} 秒前`;
    if (diff < 3600) return `${Math.floor(diff / 60)} 分前`;
    const date = new Date(epochSeconds * 1000);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }

  function vibrate() { if (navigator.vibrate) navigator.vibrate(15); }

  // ---- 画面切替 --------------------------------------------------------------
  function showPair(message, isError = true) {
    els.mainView.hidden = true;
    els.pairView.hidden = false;
    els.pairMsg.textContent = message || '';
    els.pairMsg.className = 'msg ' + (message ? (isError ? 'error' : 'ok') : '');
    setConn('', '未接続');
    setTimeout(() => els.codeInput.focus({ preventScroll: true }), 50);
  }

  function showMain() {
    els.pairView.hidden = true;
    els.mainView.hidden = false;
    els.pcName.textContent = storage.get(PC_NAME_KEY) ? `接続先: ${storage.get(PC_NAME_KEY)}` : '';
    els.a2hs.hidden = !(isIOS && !standalone);
    connect();
  }

  function unpair(message) {
    disconnect();
    token = null;
    storage.remove(TOKEN_KEY);
    showPair(message);
  }

  // ---- API ---------------------------------------------------------------------
  async function api(path, options = {}) {
    const headers = Object.assign({ 'X-Token': token || '' }, options.headers || {});
    let response;
    try {
      response = await fetch(path, Object.assign({ cache: 'no-store' }, options, { headers }));
    } catch {
      throw new Error('PC に接続できません。iPhone の Tailscale がオンか、PC で ClipBridge が起動しているか確認してください。');
    }
    if (response.status === 401) {
      unpair('接続が解除されました。PC の画面に表示されている新しいコードを入力してください。');
      throw new Error('接続が解除されました');
    }
    let data = {};
    try { data = await response.json(); } catch { /* 本文なし */ }
    if (!response.ok) throw new Error(data.message || `エラー (${response.status})`);
    return data;
  }

  // ---- ペアリング --------------------------------------------------------------
  els.codeInput.addEventListener('input', () => {
    els.codeInput.value = els.codeInput.value.replace(/\D/g, '').slice(0, 6);
    if (els.codeInput.value.length === 6) els.pairForm.requestSubmit();
  });

  els.pairForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = els.codeInput.value;
    if (code.length !== 6) { els.pairMsg.textContent = '6 桁の数字を入力してください。'; els.pairMsg.className = 'msg error'; return; }
    els.pairButton.disabled = true;
    els.pairMsg.textContent = '接続しています…';
    els.pairMsg.className = 'msg';
    try {
      const response = await fetch('/api/pair', {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || 'コードが違います。');
      storage.set(TOKEN_KEY, data.token);
      if (data.pc_name) storage.set(PC_NAME_KEY, data.pc_name);
      // トークン付き URL で読み直し、manifest（ホーム画面追加用）にもトークンを反映させる
      location.replace('/?t=' + encodeURIComponent(data.token));
    } catch (error) {
      els.pairMsg.textContent = error instanceof TypeError
        ? 'PC に接続できません。iPhone の Tailscale がオンか、PC で ClipBridge が起動しているか確認してください。'
        : error.message;
      els.pairMsg.className = 'msg error';
      els.codeInput.value = '';
      els.codeInput.focus();
    } finally {
      els.pairButton.disabled = false;
    }
  });

  // ---- PC → iPhone -------------------------------------------------------------
  function render(data) {
    current = data;
    const changed = data.hash !== lastHash;
    lastHash = data.hash;
    if (data.kind === 'text' && data.text) {
      els.clipText.textContent = data.text;
      els.clipText.classList.remove('empty');
      els.copyBtn.disabled = false;
    } else {
      els.clipText.textContent = data.kind === 'other' ? '（テキスト以外の内容です）' : '（空です）';
      els.clipText.classList.add('empty');
      els.copyBtn.disabled = true;
    }
    els.clipMeta.textContent = `${data.length.toLocaleString()} 文字 ・ ${relativeTime(data.updated)}`;
    if (changed && !firstEvent) {
      els.newBadge.hidden = data.source === 'phone';
      els.clipText.classList.remove('flash');
      void els.clipText.offsetWidth;
      els.clipText.classList.add('flash');
      els.copyFallback.hidden = true;
      els.copyHint.hidden = true;
    }
    firstEvent = false;
  }

  setInterval(() => { if (current) els.clipMeta.textContent = `${current.length.toLocaleString()} 文字 ・ ${relativeTime(current.updated)}`; }, 10000);

  els.copyBtn.addEventListener('click', async () => {
    if (!current || !current.text) return;
    els.newBadge.hidden = true;
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('unsupported');
      await navigator.clipboard.writeText(current.text);
      vibrate();
      toast(`iPhone にコピーしました（${current.length.toLocaleString()} 文字）`);
      els.copyFallback.hidden = true;
      els.copyHint.hidden = true;
    } catch {
      // 自動コピー不可: 選択状態のテキスト欄を出して手動コピーしてもらう
      els.copyFallback.value = current.text;
      els.copyFallback.hidden = false;
      els.copyHint.hidden = false;
      els.copyFallback.focus();
      els.copyFallback.setSelectionRange(0, els.copyFallback.value.length);
      try {
        if (document.execCommand('copy')) {
          toast('iPhone にコピーしました');
          els.copyFallback.hidden = true;
          els.copyHint.hidden = true;
        }
      } catch { /* 手動にフォールバック */ }
    }
  });

  // ---- iPhone → PC -------------------------------------------------------------
  async function send(text) {
    const data = await api('/api/clip', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: text,
    });
    vibrate();
    els.sendMsg.textContent = `PC へ送りました（${data.length.toLocaleString()} 文字）`;
    els.sendMsg.className = 'msg ok';
    toast('PC へ送りました');
    return data;
  }

  els.sendBtn.addEventListener('click', async () => {
    els.sendBtn.disabled = true;
    els.sendMsg.textContent = '';
    els.sendMsg.className = 'msg';
    try {
      if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error('unsupported');
      const text = await navigator.clipboard.readText();
      if (!text) {
        els.sendMsg.textContent = 'iPhone のクリップボードにテキストがありません。';
        els.sendMsg.className = 'msg error';
        return;
      }
      await send(text);
    } catch (error) {
      if (error && /unsupported|NotAllowed|denied|permission/i.test(String(error.name || error.message))) {
        els.sendMsg.textContent = '読み取りが許可されませんでした。下の欄に貼り付けてください。';
        els.sendMsg.className = 'msg error';
        els.sendText.focus();
      } else if (error && error.message) {
        els.sendMsg.textContent = error.message;
        els.sendMsg.className = 'msg error';
      }
    } finally {
      els.sendBtn.disabled = false;
    }
  });

  els.sendText.addEventListener('input', () => {
    clearTimeout(sendTimer);
    const text = els.sendText.value;
    if (!text) return;
    sendTimer = setTimeout(async () => {
      try {
        await send(text);
        els.sendText.value = '';
      } catch (error) {
        els.sendMsg.textContent = error.message;
        els.sendMsg.className = 'msg error';
      }
    }, 450);
  });

  // ---- SSE 接続 ----------------------------------------------------------------
  function connect() {
    if (!token || eventSource) return;
    setConn('warn', '接続中…');
    eventSource = new EventSource('/api/events?t=' + encodeURIComponent(token));
    eventSource.addEventListener('open', () => setConn('ok', '接続中'));
    eventSource.addEventListener('clip', (event) => {
      try { render(JSON.parse(event.data)); } catch { /* ignore */ }
    });
    eventSource.addEventListener('error', async () => {
      setConn('warn', '再接続中…');
      // EventSource からは 401 を判別できないため、通常の API で確認する
      try { await api('/api/clip'); } catch { /* unpair 済み or 一時的な切断 */ }
    });
  }

  function disconnect() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    clearTimeout(reconnectTimer);
  }

  async function refresh() {
    if (!token) return;
    try { render(await api('/api/clip')); } catch { /* 接続不可は pill 側で表示 */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      disconnect();
      if (token) { refresh(); connect(); }
    } else {
      disconnect();
    }
  });
  window.addEventListener('pageshow', (event) => { if (event.persisted && token) { disconnect(); refresh(); connect(); } });

  // ---- 解除 ----------------------------------------------------------------------
  els.unpairBtn.addEventListener('click', () => {
    unpair('接続を解除しました。再接続するには PC の画面に表示されているコードを入力してください。');
    els.pairMsg.className = 'msg';
  });

  // ---- 起動 ----------------------------------------------------------------------
  if (pairResult === 'invalid') {
    storage.remove(TOKEN_KEY);
    token = null;
    showPair('このリンクは使えなくなっています。PC の画面に表示されている最新のコードを入力してください。');
  } else if (token) {
    showMain();
    refresh();
  } else {
    showPair('');
  }
})();
