/* ClipBridge iPhone 側クライアント
 *
 * 複数 PC: この画面を配っている PC（ハブ）だけでなく、同じ tailnet の他の PC にも
 * 直接つなぐ。PC ごとのトークンは localStorage にまとめて保存し、上部のピルで切り替える。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PCS_KEY = 'clipbridge.pcs';       // [{ origin, name, token }]
  const ACTIVE_KEY = 'clipbridge.active'; // 使用中の PC の origin
  const LEGACY_TOKEN_KEY = 'clipbridge.token';     // 2.0 までの保存形式
  const LEGACY_NAME_KEY = 'clipbridge.pcName';

  const els = {
    conn: $('conn'),
    pcSwitch: $('pcSwitch'),
    pcSwitchName: $('pcSwitchName'),
    pcMenu: $('pcMenu'),
    pcMenuStatus: $('pcMenuStatus'),
    pcList: $('pcList'),
    addPcBtn: $('addPcBtn'),
    pairView: $('pairView'),
    mainView: $('mainView'),
    addView: $('addView'),
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
    addFromSettingsBtn: $('addFromSettingsBtn'),
    peerList: $('peerList'),
    peersRefreshBtn: $('peersRefreshBtn'),
    peerUrlInput: $('peerUrlInput'),
    peerUrlBtn: $('peerUrlBtn'),
    addForm: $('addForm'),
    addTargetName: $('addTargetName'),
    addCodeInput: $('addCodeInput'),
    addButton: $('addButton'),
    addMsg: $('addMsg'),
    addCancelBtn: $('addCancelBtn'),
    toast: $('toast'),
  };

  // ---- 保存領域（ホーム画面版でも使えるよう例外を握りつぶす） ------------
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  function loadPcs() {
    try {
      const list = JSON.parse(storage.get(PCS_KEY) || '[]');
      if (Array.isArray(list)) {
        return list.filter((pc) => pc && typeof pc.origin === 'string' && typeof pc.token === 'string');
      }
    } catch { /* 壊れていたら空にする */ }
    return [];
  }

  let pcs = loadPcs();
  // 2.0 までの単一トークンを引き継ぐ
  const legacyToken = storage.get(LEGACY_TOKEN_KEY);
  if (legacyToken && !pcs.some((pc) => pc.origin === location.origin)) {
    pcs.push({ origin: location.origin, name: storage.get(LEGACY_NAME_KEY) || '', token: legacyToken });
  }
  if (legacyToken || storage.get(LEGACY_NAME_KEY)) {
    storage.remove(LEGACY_TOKEN_KEY);
    storage.remove(LEGACY_NAME_KEY);
    savePcs();
  }

  function savePcs() { storage.set(PCS_KEY, JSON.stringify(pcs)); }

  function upsertPc(pc) {
    const index = pcs.findIndex((item) => item.origin === pc.origin);
    if (index >= 0) pcs[index] = Object.assign({}, pcs[index], pc);
    else pcs.push(pc);
    savePcs();
    return pcs.find((item) => item.origin === pc.origin);
  }

  function hostLabel(origin) {
    try { return new URL(origin).hostname.split('.')[0]; } catch { return origin; }
  }

  function displayName(pc) { return (pc && (pc.name || hostLabel(pc.origin))) || '—'; }

  // ---- URL からの引き継ぎ（QR / manifest start_url / 失敗時のメッセージ） --
  const url = new URL(location.href);
  const tokenFromUrl = url.searchParams.get('t');
  const pairResult = url.searchParams.get('pair');
  if (tokenFromUrl) {
    upsertPc({ origin: location.origin, token: tokenFromUrl, name: (pcs.find((pc) => pc.origin === location.origin) || {}).name || '' });
    storage.set(ACTIVE_KEY, location.origin);
  }
  if (tokenFromUrl || pairResult) history.replaceState(null, '', '/');

  let active = pcs.find((pc) => pc.origin === storage.get(ACTIVE_KEY)) || pcs[0] || null;
  const pcStatus = {};           // origin -> { state: 'ok' | 'warn' | 'err' | '', label }
  let current = null;            // 最新の PC クリップボード
  let lastHash = null;
  let eventSource = null;
  let toastTimer = null;
  let sendTimer = null;
  let firstEvent = true;
  let addTarget = null;          // 追加しようとしている PC { origin, name }

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

  function setConn(state, label, pc = active) {
    if (pc) pcStatus[pc.origin] = { state, label };
    if (!active) {
      els.conn.className = 'pill ' + state;
      els.conn.textContent = label;
      return;
    }
    if (!pc || pc.origin !== active.origin) return;
    els.pcSwitch.className = 'pill switcher ' + state;
    els.pcSwitch.title = label;
    els.pcSwitchName.textContent = displayName(active);
    els.pcMenuStatus.textContent = label;
    renderPcList();
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
    disconnect();
    closeMenu();
    els.mainView.hidden = true;
    els.addView.hidden = true;
    els.pairView.hidden = false;
    els.pcSwitch.hidden = true;
    els.conn.hidden = false;
    els.pairMsg.textContent = message || '';
    els.pairMsg.className = 'msg ' + (message ? (isError ? 'error' : 'ok') : '');
    setConn('', '未接続', null);
    setTimeout(() => els.codeInput.focus({ preventScroll: true }), 50);
  }

  function showMain() {
    els.pairView.hidden = true;
    els.addView.hidden = true;
    els.mainView.hidden = false;
    els.conn.hidden = true;
    els.pcSwitch.hidden = false;
    els.pcSwitchName.textContent = displayName(active);
    els.pcName.textContent = active ? `接続先: ${displayName(active)}` : '';
    els.a2hs.hidden = !(isIOS && !standalone);
    renderPcList();
    connect();
  }

  function resetClip() {
    current = null;
    lastHash = null;
    firstEvent = true;
    els.clipText.textContent = '（読み込み中…）';
    els.clipText.classList.add('empty');
    els.clipMeta.textContent = '';
    els.newBadge.hidden = true;
    els.copyBtn.disabled = true;
    els.copyFallback.hidden = true;
    els.copyHint.hidden = true;
    els.sendMsg.textContent = '';
    els.sendMsg.className = 'msg';
  }

  function switchTo(origin) {
    const pc = pcs.find((item) => item.origin === origin);
    if (!pc) return;
    disconnect();
    active = pc;
    storage.set(ACTIVE_KEY, origin);
    resetClip();
    closeMenu();
    showMain();
    refresh();
  }

  function removePc(origin, message) {
    const removed = pcs.find((pc) => pc.origin === origin);
    pcs = pcs.filter((pc) => pc.origin !== origin);
    delete pcStatus[origin];
    savePcs();
    if (active && active.origin === origin) {
      disconnect();
      active = pcs[0] || null;
      if (active) storage.set(ACTIVE_KEY, active.origin); else storage.remove(ACTIVE_KEY);
      resetClip();
    }
    if (!active) {
      showPair(message);
      return;
    }
    showMain();
    refresh();
    if (message) toast(message.replace(/。.*$/, '。'));
    else if (removed) toast(`${displayName(removed)} の接続を解除しました`);
  }

  // ---- API ---------------------------------------------------------------------
  async function api(path, options = {}, pc = active) {
    if (!pc) throw new Error('PC が選ばれていません');
    const headers = Object.assign({ 'X-Token': pc.token || '' }, options.headers || {});
    let response;
    try {
      response = await fetch(pc.origin + path, Object.assign({ cache: 'no-store', mode: 'cors' }, options, { headers }));
    } catch {
      setConn('err', 'オフライン', pc);
      throw new Error(`${displayName(pc)} に接続できません。iPhone の Tailscale がオンか、PC で ClipBridge が起動しているか確認してください。`);
    }
    if (response.status === 401) {
      removePc(pc.origin, `${displayName(pc)} との接続が解除されました。PC の画面に表示されている新しいコードで接続し直してください。`);
      throw new Error('接続が解除されました');
    }
    let data = {};
    try { data = await response.json(); } catch { /* 本文なし */ }
    if (!response.ok) throw new Error(data.message || `エラー (${response.status})`);
    return data;
  }

  async function pairWith(origin, code) {
    const response = await fetch(origin + '/api/pair', {
      method: 'POST',
      cache: 'no-store',
      mode: 'cors',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || 'コードが違います。');
    return data;
  }

  // ---- 初回ペアリング（この画面を配っている PC と） -------------------------
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
      const data = await pairWith(location.origin, code);
      upsertPc({ origin: location.origin, name: data.pc_name || '', token: data.token });
      storage.set(ACTIVE_KEY, location.origin);
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

  // ---- PC の切り替えメニュー ---------------------------------------------------
  function renderPcList() {
    els.pcList.innerHTML = '';
    for (const pc of pcs) {
      const li = document.createElement('li');
      li.className = 'pc-item' + (active && pc.origin === active.origin ? ' active' : '');
      const status = pcStatus[pc.origin] || { state: '', label: '' };
      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'pc-pick';
      pick.setAttribute('role', 'menuitemradio');
      pick.setAttribute('aria-checked', active && pc.origin === active.origin ? 'true' : 'false');
      const dot = document.createElement('span');
      dot.className = 'dot ' + status.state;
      const text = document.createElement('span');
      text.className = 'pc-text';
      const name = document.createElement('span');
      name.className = 'pc-name';
      name.textContent = displayName(pc);
      const sub = document.createElement('span');
      sub.className = 'pc-sub';
      const host = hostLabel(pc.origin);
      sub.textContent = [host.toLowerCase() !== displayName(pc).toLowerCase() ? host : '', status.label].filter(Boolean).join(' ・ ');
      text.append(name, sub);
      pick.append(dot, text);
      pick.addEventListener('click', () => { if (!active || pc.origin !== active.origin) switchTo(pc.origin); else closeMenu(); });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'link pc-remove';
      remove.textContent = '解除';
      remove.setAttribute('aria-label', `${displayName(pc)} の接続を解除`);
      remove.addEventListener('click', (event) => {
        event.stopPropagation();
        if (window.confirm(`${displayName(pc)} の接続を解除しますか？`)) removePc(pc.origin);
      });
      li.append(pick, remove);
      els.pcList.appendChild(li);
    }
  }

  async function probeAll() {
    // メニューを開いたとき、各 PC が今つながるかを軽く確認する（使用中の PC は SSE の状態をそのまま使う）
    await Promise.all(pcs.filter((pc) => !active || pc.origin !== active.origin).map(async (pc) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2500);
      try {
        const response = await fetch(pc.origin + '/api/health', { cache: 'no-store', mode: 'cors', signal: controller.signal });
        const data = await response.json().catch(() => ({}));
        if (data.pc_name && data.pc_name !== pc.name) upsertPc({ origin: pc.origin, name: data.pc_name });
        pcStatus[pc.origin] = { state: response.ok ? 'ok' : 'err', label: response.ok ? 'オンライン' : 'エラー' };
      } catch {
        pcStatus[pc.origin] = { state: 'err', label: 'オフライン' };
      } finally {
        clearTimeout(timer);
      }
    }));
    renderPcList();
  }

  function openMenu() {
    renderPcList();
    els.pcMenu.hidden = false;
    els.pcSwitch.setAttribute('aria-expanded', 'true');
    probeAll();
  }

  function closeMenu() {
    els.pcMenu.hidden = true;
    els.pcSwitch.setAttribute('aria-expanded', 'false');
  }

  els.pcSwitch.addEventListener('click', (event) => {
    event.stopPropagation();
    if (els.pcMenu.hidden) openMenu(); else closeMenu();
  });
  document.addEventListener('click', (event) => {
    if (!els.pcMenu.hidden && !els.pcMenu.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenu(); });

  // ---- PC の追加 ------------------------------------------------------------------
  function showAdd() {
    closeMenu();
    disconnect();
    els.mainView.hidden = true;
    els.pairView.hidden = true;
    els.addView.hidden = false;
    els.addForm.hidden = true;
    addTarget = null;
    els.addMsg.textContent = '';
    els.addMsg.className = 'msg';
    els.peerUrlInput.value = '';
    loadPeers();
  }

  function leaveAdd() {
    els.addView.hidden = true;
    if (active) { showMain(); refresh(); } else showPair('');
  }

  function peerItem(label, sub, button) {
    const li = document.createElement('li');
    li.className = 'pc-item';
    const text = document.createElement('span');
    text.className = 'pc-text';
    const name = document.createElement('span');
    name.className = 'pc-name';
    name.textContent = label;
    const detail = document.createElement('span');
    detail.className = 'pc-sub';
    detail.textContent = sub;
    text.append(name, detail);
    li.append(text);
    if (button) li.append(button);
    return li;
  }

  async function loadPeers(refresh = false) {
    els.peerList.innerHTML = '';
    els.peerList.appendChild(peerItem('探しています…', '同じ Tailscale ネットワークの PC に問い合わせています', null));
    els.peersRefreshBtn.disabled = true;
    // 使用中の PC がオフラインなら、登録済みの別の PC に聞く
    const sources = [active, ...pcs.filter((pc) => !active || pc.origin !== active.origin)].filter(Boolean);
    let result = null;
    let lastError = null;
    for (const source of sources) {
      try {
        result = await api('/api/peers' + (refresh ? '?refresh=1' : ''), {}, source);
        break;
      } catch (error) {
        lastError = error;
      }
    }
    els.peersRefreshBtn.disabled = false;
    els.peerList.innerHTML = '';
    if (!result) {
      els.peerList.appendChild(peerItem('PC を探せませんでした', lastError ? lastError.message : '登録済みの PC に接続できません。下の欄に URL を入力して追加できます。', null));
      return;
    }
    const known = new Set(pcs.map((pc) => pc.origin));
    const candidates = result.peers.filter((peer) => { try { return !known.has(new URL(peer.url).origin); } catch { return false; } });
    if (!candidates.length) {
      els.peerList.appendChild(peerItem(
        result.peers.length ? '見つかった PC はすべて追加済みです' : 'ほかの PC が見つかりません',
        '追加したい PC で ClipBridge を起動し、Tailscale に接続してから「再検索」を押してください。',
        null,
      ));
      return;
    }
    for (const peer of candidates) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn';
      button.textContent = '選ぶ';
      button.addEventListener('click', () => selectTarget(new URL(peer.url).origin, peer.name));
      els.peerList.appendChild(peerItem(peer.name, [peer.host !== peer.name ? peer.host : '', peer.version ? `v${peer.version}` : ''].filter(Boolean).join(' ・ '), button));
    }
  }

  function selectTarget(origin, name) {
    addTarget = { origin, name: name || hostLabel(origin) };
    els.addTargetName.textContent = `${addTarget.name}（${new URL(origin).host}）`;
    els.addForm.hidden = false;
    els.addCodeInput.value = '';
    els.addMsg.textContent = '';
    els.addMsg.className = 'msg';
    els.addForm.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => els.addCodeInput.focus({ preventScroll: true }), 300);
  }

  els.peersRefreshBtn.addEventListener('click', () => loadPeers(true));
  els.peerUrlBtn.addEventListener('click', () => {
    let parsed;
    try {
      parsed = new URL(els.peerUrlInput.value.trim());
    } catch {
      els.addMsg.textContent = 'URL の形式が正しくありません。https:// から入力してください。';
      els.addMsg.className = 'msg error';
      return;
    }
    if (parsed.protocol !== 'https:') {
      els.addMsg.textContent = 'https:// の URL を入力してください。';
      els.addMsg.className = 'msg error';
      return;
    }
    selectTarget(parsed.origin, hostLabel(parsed.origin));
  });

  els.addCodeInput.addEventListener('input', () => {
    els.addCodeInput.value = els.addCodeInput.value.replace(/\D/g, '').slice(0, 6);
    if (els.addCodeInput.value.length === 6) els.addForm.requestSubmit();
  });

  els.addForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!addTarget) return;
    const code = els.addCodeInput.value;
    if (code.length !== 6) { els.addMsg.textContent = '6 桁の数字を入力してください。'; els.addMsg.className = 'msg error'; return; }
    els.addButton.disabled = true;
    els.addMsg.textContent = `${addTarget.name} に接続しています…`;
    els.addMsg.className = 'msg';
    try {
      const data = await pairWith(addTarget.origin, code);
      const pc = upsertPc({ origin: addTarget.origin, name: data.pc_name || addTarget.name, token: data.token });
      toast(`${displayName(pc)} を追加しました`);
      switchTo(pc.origin);
    } catch (error) {
      els.addMsg.textContent = error instanceof TypeError
        ? `${addTarget.name} に接続できません。その PC で ClipBridge が起動しているか、Tailscale がオンか確認してください。`
        : error.message;
      els.addMsg.className = 'msg error';
      els.addCodeInput.value = '';
      els.addCodeInput.focus();
    } finally {
      els.addButton.disabled = false;
    }
  });

  els.addCancelBtn.addEventListener('click', leaveAdd);
  els.addPcBtn.addEventListener('click', showAdd);
  els.addFromSettingsBtn.addEventListener('click', showAdd);

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
    const target = active;
    const data = await api('/api/clip', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: text,
    }, target);
    vibrate();
    els.sendMsg.textContent = `${displayName(target)} へ送りました（${data.length.toLocaleString()} 文字）`;
    els.sendMsg.className = 'msg ok';
    toast(`${displayName(target)} へ送りました`);
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
    if (!active || eventSource) return;
    const pc = active;
    setConn('warn', '接続中…', pc);
    eventSource = new EventSource(pc.origin + '/api/events?t=' + encodeURIComponent(pc.token));
    eventSource.addEventListener('open', () => setConn('ok', '接続中', pc));
    eventSource.addEventListener('clip', (event) => {
      try { render(JSON.parse(event.data)); } catch { /* ignore */ }
    });
    eventSource.addEventListener('error', async () => {
      setConn('warn', '再接続中…', pc);
      // EventSource からは 401 を判別できないため、通常の API で確認する
      try { await api('/api/clip', {}, pc); } catch { /* 解除済み or 一時的な切断（オフライン表示は api 側） */ }
    });
    if (!pc.name) {
      fetch(pc.origin + '/api/health', { cache: 'no-store', mode: 'cors' })
        .then((response) => response.json())
        .then((data) => { if (data.pc_name) { upsertPc({ origin: pc.origin, name: data.pc_name }); if (active && active.origin === pc.origin) showMainLabels(); } })
        .catch(() => {});
    }
  }

  function showMainLabels() {
    els.pcSwitchName.textContent = displayName(active);
    els.pcName.textContent = active ? `接続先: ${displayName(active)}` : '';
    renderPcList();
  }

  function disconnect() {
    if (eventSource) { eventSource.close(); eventSource = null; }
  }

  async function refresh() {
    if (!active) return;
    try { render(await api('/api/clip')); } catch { /* 接続不可は pill 側で表示 */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      disconnect();
      if (active && !els.mainView.hidden) { refresh(); connect(); }
    } else {
      disconnect();
    }
  });
  window.addEventListener('pageshow', (event) => { if (event.persisted && active && !els.mainView.hidden) { disconnect(); refresh(); connect(); } });

  // ---- 解除 ----------------------------------------------------------------------
  els.unpairBtn.addEventListener('click', () => {
    if (!active) return;
    if (!window.confirm(`${displayName(active)} の接続を解除しますか？`)) return;
    removePc(active.origin, pcs.length <= 1 ? '接続を解除しました。再接続するには PC の画面に表示されているコードを入力してください。' : '');
    if (!active) els.pairMsg.className = 'msg';
  });

  // ---- 起動 ----------------------------------------------------------------------
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* 省略可能な機能 */ });
  }

  if (pairResult === 'invalid') {
    if (active) {
      showMain();
      refresh();
      toast('このリンクは使えなくなっています。PC の画面の最新のコードを使ってください。', 'error');
    } else {
      showPair('このリンクは使えなくなっています。PC の画面に表示されている最新のコードを入力してください。');
    }
  } else if (active) {
    showMain();
    refresh();
  } else {
    showPair('');
  }
})();
