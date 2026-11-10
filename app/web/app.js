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
    clipImageWrap: $('clipImageWrap'),
    clipImage: $('clipImage'),
    clipImageNote: $('clipImageNote'),
    copyBtn: $('copyBtn'),
    shareImageBtn: $('shareImageBtn'),
    imageSaveHint: $('imageSaveHint'),
    copyFallback: $('copyFallback'),
    copyHint: $('copyHint'),
    sendBtn: $('sendBtn'),
    sendText: $('sendText'),
    sendMsg: $('sendMsg'),
    imageInput: $('imageInput'),
    imageBtn: $('imageBtn'),
    imageModeInputs: document.querySelectorAll('input[name="imageMode"]'),
    imageProgress: $('imageProgress'),
    imageBar: $('imageBar'),
    imageMsg: $('imageMsg'),
    imageDetail: $('imageDetail'),
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
  let preview = null;            // PC の画像のプレビュー { id, origin, blob, url }
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
    dropPreview();
    els.clipText.hidden = false;
    els.clipText.textContent = '（読み込み中…）';
    els.clipText.classList.add('empty');
    els.clipImageWrap.hidden = true;
    els.shareImageBtn.hidden = true;
    els.imageSaveHint.hidden = true;
    els.clipMeta.textContent = '';
    els.newBadge.hidden = true;
    els.copyBtn.disabled = true;
    els.copyFallback.hidden = true;
    els.copyHint.hidden = true;
    els.sendMsg.textContent = '';
    els.sendMsg.className = 'msg';
    setImageMsg('');
    els.imageDetail.hidden = true;
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
  function metaText(data) {
    const when = relativeTime(data.updated);
    if (data.kind === 'image') {
      if (!data.image) return `画像 ・ ${when}`;
      const dims = data.image.width && data.image.height ? `${data.image.width}×${data.image.height} ・ ` : '';
      return `画像 ${dims}${formatBytes(data.image.size)} ・ ${when}`;
    }
    return `${data.length.toLocaleString()} 文字 ・ ${when}`;
  }

  function render(data) {
    current = data;
    const changed = data.hash !== lastHash;
    lastHash = data.hash;
    const hasImage = data.kind === 'image' && !!data.image;
    els.clipText.hidden = hasImage;
    els.clipImageWrap.hidden = !hasImage;
    // iOS の Safari では Web ページから写真に直接保存できないため、確実に使える「画像を長押し」を案内する
    els.shareImageBtn.hidden = !(hasImage && navigator.share && navigator.canShare);
    els.imageSaveHint.hidden = !hasImage;
    if (!hasImage || changed) els.imageSaveHint.textContent = SAVE_HINT;
    if (data.kind === 'text' && data.text) {
      els.clipText.textContent = data.text;
      els.clipText.classList.remove('empty');
      els.copyBtn.disabled = false;
    } else if (data.kind === 'image') {
      els.clipText.textContent = '（画像が入っていますが、表示できる形式ではありません）';
      els.clipText.classList.add('empty');
      els.copyBtn.disabled = !hasImage;
    } else {
      els.clipText.textContent = data.kind === 'other' ? '（テキスト以外の内容です）' : '（空です）';
      els.clipText.classList.add('empty');
      els.copyBtn.disabled = true;
    }
    if (hasImage) showPreview(active, data.image); else dropPreview();
    els.clipMeta.textContent = metaText(data);
    if (changed && !firstEvent) {
      els.newBadge.hidden = data.source === 'phone';
      const target = hasImage ? els.clipImageWrap : els.clipText;
      target.classList.remove('flash');
      void target.offsetWidth;
      target.classList.add('flash');
      els.copyFallback.hidden = true;
      els.copyHint.hidden = true;
    }
    firstEvent = false;
  }

  setInterval(() => { if (current) els.clipMeta.textContent = metaText(current); }, 10000);

  const SAVE_HINT = '写真に保存するには、上の画像を長押しして「“写真”に保存」を選びます。';

  // PC のクリップボードにある画像を取り寄せて表示する（コピー・共有はこの blob を使う）
  function dropPreview() {
    if (preview && preview.url) {
      // 長押しメニューの「“写真”に保存」が進行中でも壊れないよう、URL の解放は少し遅らせる
      const url = preview.url;
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    preview = null;
    els.clipImage.removeAttribute('src');
    els.clipImageNote.textContent = '';
  }

  async function fetchImageBlob(pc, image) {
    let response;
    try {
      response = await fetch(`${pc.origin}/api/image?id=${encodeURIComponent(image.id)}`, { cache: 'no-store', mode: 'cors', headers: { 'X-Token': pc.token || '' } });
    } catch {
      throw new Error('画像を取得できません（接続を確認してください）');
    }
    if (response.status === 401) {
      removePc(pc.origin, `${displayName(pc)} との接続が解除されました。PC の画面に表示されている新しいコードで接続し直してください。`);
      throw new Error('接続が解除されました');
    }
    if (!response.ok) throw new Error('画像を取得できませんでした（PC 側の内容が変わった可能性があります）');
    return response.blob();
  }

  async function showPreview(pc, image) {
    if (!pc) return;
    if (preview && preview.id === image.id && preview.origin === pc.origin) return;
    dropPreview();
    const pending = { id: image.id, origin: pc.origin, blob: null, url: null };
    preview = pending;
    els.clipImageNote.textContent = '読み込んでいます…';
    try {
      const blob = await fetchImageBlob(pc, image);
      if (preview !== pending) return; // その間に別の内容になった
      pending.blob = blob;
      pending.url = URL.createObjectURL(blob);
      els.clipImage.src = pending.url;
      els.clipImageNote.textContent = '';
    } catch (error) {
      if (preview === pending) els.clipImageNote.textContent = error.message;
    }
  }

  async function copyImage() {
    els.newBadge.hidden = true;
    const blob = preview && preview.blob;
    if (!blob) { toast('画像をまだ読み込んでいます。少し待ってからもう一度押してください。', 'error'); return; }
    try {
      if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) throw new Error('unsupported');
      // Safari が受け付ける画像形式は PNG だけ。別形式なら変換結果の Promise を渡す（ユーザー操作の範囲内で write を呼ぶため）
      const png = blob.type === 'image/png' ? blob : toPng(blob);
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
      vibrate();
      toast('画像を iPhone にコピーしました');
      els.imageSaveHint.textContent = SAVE_HINT;
    } catch {
      els.imageSaveHint.textContent = '自動でコピーできませんでした。上の画像を長押しして「コピー」を選んでください（「“写真”に保存」で写真にも入ります）。';
      els.imageSaveHint.hidden = false;
    }
  }

  els.shareImageBtn.addEventListener('click', async () => {
    // 共有シート（AirDrop / メッセージ / “ファイル”に保存 など）。写真への保存は長押しメニューの方が確実
    const blob = preview && preview.blob;
    if (!blob) { toast('画像をまだ読み込んでいます。少し待ってからもう一度押してください。', 'error'); return; }
    const ext = (blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
    const file = new File([blob], `ClipBridge-${fileStamp()}.${ext}`, { type: blob.type || 'image/png' });
    if (!(navigator.canShare && navigator.canShare({ files: [file] }))) {
      toast('この iOS では共有シートに画像を渡せません。画像を長押しして保存してください。', 'error');
      return;
    }
    try {
      await navigator.share({ files: [file], title: 'ClipBridge' });
    } catch (error) {
      if (!error || error.name !== 'AbortError') toast('共有できませんでした', 'error');
    }
  });

  els.copyBtn.addEventListener('click', async () => {
    if (!current) return;
    if (current.kind === 'image') { await copyImage(); return; }
    if (!current.text) return;
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

  async function readPhoneClipboard() {
    // 画像も読めるよう read() を優先し、非対応・失敗なら readText() に戻す
    if (navigator.clipboard.read) {
      let items = null;
      try {
        items = await navigator.clipboard.read();
      } catch (error) {
        if (/NotAllowed|denied|permission/i.test(String(error && (error.name + ' ' + error.message)))) throw error;
      }
      if (items) {
        for (const item of items) {
          const imageType = item.types.find((type) => type.startsWith('image/'));
          if (imageType) {
            const blob = await item.getType(imageType);
            const ext = (imageType.split('/')[1] || 'png').replace('jpeg', 'jpg');
            return { image: new File([blob], `clipboard.${ext}`, { type: imageType }) };
          }
        }
        for (const item of items) {
          if (item.types.includes('text/plain')) return { text: await (await item.getType('text/plain')).text() };
        }
        return {};
      }
    }
    if (!navigator.clipboard.readText) throw new Error('unsupported');
    return { text: await navigator.clipboard.readText() };
  }

  els.sendBtn.addEventListener('click', async () => {
    els.sendBtn.disabled = true;
    els.sendMsg.textContent = '';
    els.sendMsg.className = 'msg';
    try {
      if (!navigator.clipboard) throw new Error('unsupported');
      const content = await readPhoneClipboard();
      if (content.image) {
        await sendImages([content.image], 'clipboard');
        return;
      }
      if (!content.text) {
        els.sendMsg.textContent = 'iPhone のクリップボードにテキストも画像もありません。';
        els.sendMsg.className = 'msg error';
        return;
      }
      await send(content.text);
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

  // ---- iPhone → PC（画像） ------------------------------------------------------
  const IMAGE_MODE_KEY = 'clipbridge.imageMode'; // save: 保存 + クリップボード / clip: クリップボードだけ
  const NATIVE_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/bmp']); // Windows (GDI+) がそのまま開ける形式
  let imageBusy = false;

  function imageMode() { return storage.get(IMAGE_MODE_KEY) === 'clip' ? 'clip' : 'save'; }
  for (const input of els.imageModeInputs) {
    input.checked = input.value === imageMode();
    input.addEventListener('change', () => { if (input.checked) storage.set(IMAGE_MODE_KEY, input.value); });
  }

  function formatBytes(n) {
    if (!n) return '0 KB';
    return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;
  }

  function fileStamp() {
    const d = new Date();
    const p = (v) => String(v).padStart(2, '0');
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  }

  function setImageMsg(text, kind = '') {
    els.imageMsg.textContent = text;
    els.imageMsg.className = 'msg ' + kind;
  }

  function setProgress(ratio) {
    const pct = Math.round(Math.max(0, Math.min(1, ratio)) * 100);
    els.imageBar.style.width = pct + '%';
    els.imageProgress.setAttribute('aria-valuenow', String(pct));
  }

  function loadImageElement(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode')); };
      img.src = url;
    });
  }

  async function drawToCanvas(file) {
    let source;
    try {
      source = await createImageBitmap(file, { imageOrientation: 'from-image' });
    } catch {
      source = await loadImageElement(file); // HEIC など createImageBitmap が扱えない形式も <img> なら Safari が描ける
    }
    let width = source.naturalWidth || source.width;
    let height = source.naturalHeight || source.height;
    const limit = 16 * 1024 * 1024; // iOS の canvas 上限（画素数）
    if (width * height > limit) {
      const scale = Math.sqrt(limit / (width * height));
      width = Math.floor(width * scale);
      height = Math.floor(height * scale);
    }
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d').drawImage(source, 0, 0, width, height);
    if (source.close) source.close();
    return canvas;
  }

  function canvasToBlob(canvas, type, quality) {
    return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('encode'))), type, quality));
  }

  async function toPng(blob) {
    return canvasToBlob(await drawToCanvas(blob), 'image/png');
  }

  async function prepareImage(file) {
    // HEIC / WebP など Windows 側で開けない可能性のある形式は、iPhone 側で JPEG（透過がありうる形式は PNG）に変換してから送る
    if (NATIVE_IMAGE_TYPES.has(file.type)) return file;
    try {
      const canvas = await drawToCanvas(file);
      const keepAlpha = /webp|svg|avif/i.test(file.type);
      const type = keepAlpha ? 'image/png' : 'image/jpeg';
      const blob = await canvasToBlob(canvas, type, 0.92);
      const stem = (file.name || 'image').replace(/\.[^.]+$/, '') || 'image';
      return new File([blob], `${stem}.${keepAlpha ? 'png' : 'jpg'}`, { type });
    } catch {
      return file; // 変換できなければそのまま送る（PC 側で開けない形式なら警告が返る）
    }
  }

  function uploadImage(file, pc, onProgress) {
    const save = imageMode() === 'save' ? '1' : '0';
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${pc.origin}/api/image?name=${encodeURIComponent(file.name || 'image')}&save=${save}`);
      xhr.setRequestHeader('X-Token', pc.token || '');
      xhr.responseType = 'json';
      xhr.timeout = 180000;
      xhr.upload.addEventListener('progress', (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); });
      xhr.addEventListener('load', () => {
        const data = xhr.response && typeof xhr.response === 'object' ? xhr.response : {};
        if (xhr.status === 401) {
          removePc(pc.origin, `${displayName(pc)} との接続が解除されました。PC の画面に表示されている新しいコードで接続し直してください。`);
          reject(new Error('接続が解除されました'));
          return;
        }
        if (xhr.status < 200 || xhr.status >= 300) { reject(new Error(data.message || `エラー (${xhr.status})`)); return; }
        resolve(data);
      });
      xhr.addEventListener('error', () => {
        setConn('err', 'オフライン', pc);
        reject(new Error(`${displayName(pc)} に接続できません。iPhone の Tailscale がオンか、PC で ClipBridge が起動しているか確認してください。`));
      });
      xhr.addEventListener('timeout', () => reject(new Error('送信がタイムアウトしました。通信状態を確認してください。')));
      xhr.addEventListener('abort', () => reject(new Error('送信を中止しました。')));
      xhr.send(file);
    });
  }

  async function sendImages(files, from = 'picker') {
    const target = active;
    if (!target || imageBusy || !files.length) return;
    imageBusy = true;
    els.imageBtn.disabled = true;
    els.imageDetail.hidden = true;
    els.imageProgress.hidden = false;
    setProgress(0);
    let sent = 0;
    let last = null;
    let failure = null;
    try {
      for (let index = 0; index < files.length; index += 1) {
        const counter = files.length > 1 ? `（${index + 1} / ${files.length}）` : '';
        setImageMsg(`画像を準備しています…${counter}`);
        const file = await prepareImage(files[index]);
        setImageMsg(`${displayName(target)} へ送信中…${counter}`);
        setProgress(0);
        last = await uploadImage(file, target, setProgress);
        sent += 1;
      }
    } catch (error) {
      failure = error;
    }
    els.imageProgress.hidden = true;
    els.imageBtn.disabled = false;
    imageBusy = false;
    const name = displayName(target);
    if (sent && last) {
      vibrate();
      let text;
      if (last.saved && last.clipboard) {
        text = sent > 1
          ? `${sent} 枚を ${name} に保存し、最後の 1 枚をクリップボードに入れました。`
          : `${last.name}（${formatBytes(last.size)}）を ${name} に保存し、クリップボードに入れました。PC で Ctrl+V で貼り付けできます。`;
      } else if (last.saved) {
        text = `${sent > 1 ? `${sent} 枚` : last.name} を ${name} に保存しました（クリップボードには入れられませんでした: ${last.warning}）`;
      } else {
        text = sent > 1
          ? `${sent} 枚を送り、最後の 1 枚を ${name} のクリップボードに入れました（保存はしていません）。`
          : `${formatBytes(last.size)} の画像を ${name} のクリップボードに入れました（保存はしていません）。PC で Ctrl+V で貼り付けできます。`;
      }
      setImageMsg(text, 'ok');
      if (last.saved) {
        els.imageDetail.textContent = `保存先: ${last.path || last.dir}`;
        els.imageDetail.hidden = false;
      }
      toast(`${name} へ送りました`);
      if (from === 'clipboard') {
        els.sendMsg.textContent = `iPhone のコピー内容（画像）を ${name} へ送りました`;
        els.sendMsg.className = 'msg ok';
      }
    }
    if (failure) {
      setImageMsg((sent ? `${sent} 枚を送ったあとにエラー: ` : '送れませんでした: ') + failure.message, 'error');
      if (from === 'clipboard') {
        els.sendMsg.textContent = failure.message;
        els.sendMsg.className = 'msg error';
      }
    }
  }

  els.imageBtn.addEventListener('click', () => {
    if (!active || imageBusy) return;
    els.imageInput.value = '';
    els.imageInput.click();
  });
  els.imageInput.addEventListener('change', async () => {
    const files = Array.from(els.imageInput.files || []);
    if (files.length) await sendImages(files);
    els.imageInput.value = '';
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
        .then((data) => {
          if (!data.pc_name) return;
          const updated = upsertPc({ origin: pc.origin, name: data.pc_name });
          if (active && active.origin === pc.origin) { active = updated; showMainLabels(); } // 使用中の PC の表示名も更新する
        })
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
