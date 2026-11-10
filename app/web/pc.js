/* ClipBridge PC 側セットアップ画面 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const els = {
    serverPill: $('serverPill'),
    checks: $('checks'),
    serveBtn: $('serveBtn'),
    recheckBtn: $('recheckBtn'),
    serveMsg: $('serveMsg'),
    qr: $('qr'),
    publicUrl: $('publicUrl'),
    pairCode: $('pairCode'),
    newCodeBtn: $('newCodeBtn'),
    devices: $('devices'),
    startupToggle: $('startupToggle'),
    startupMsg: $('startupMsg'),
    rotateBtn: $('rotateBtn'),
    stopBtn: $('stopBtn'),
    infoLine: $('infoLine'),
    configPath: $('configPath'),
    updateVersion: $('updateVersion'),
    updateMsg: $('updateMsg'),
    updateApplyBtn: $('updateApplyBtn'),
    updateCheckBtn: $('updateCheckBtn'),
    updateLink: $('updateLink'),
    updateHint: $('updateHint'),
    imageLast: $('imageLast'),
    imageDirInput: $('imageDirInput'),
    imageSubfolder: $('imageSubfolder'),
    imageTemplate: $('imageTemplate'),
    imageExample: $('imageExample'),
    imageRulesLock: $('imageRulesLock'),
    imageSettingsBtn: $('imageSettingsBtn'),
    imageSettingsMsg: $('imageSettingsMsg'),
    openImagesBtn: $('openImagesBtn'),
    historyCount: $('historyCount'),
    historyLock: $('historyLock'),
    historyBody: $('historyBody'),
    historyToggle: $('historyToggle'),
    historySearch: $('historySearch'),
    historyList: $('historyList'),
    historyClearBtn: $('historyClearBtn'),
    historyMsg: $('historyMsg'),
    hotkeyState: $('hotkeyState'),
    hotkeyLock: $('hotkeyLock'),
    hotkeyBody: $('hotkeyBody'),
    hotkeyToggle: $('hotkeyToggle'),
    hotkeyRows: $('hotkeyRows'),
    hotkeySaveBtn: $('hotkeySaveBtn'),
    hotkeyMsg: $('hotkeyMsg'),
    proPill: $('proPill'),
    proLead: $('proLead'),
    proFeatures: $('proFeatures'),
    proApply: $('proApply'),
    proInfo: $('proInfo'),
    proHolder: $('proHolder'),
    supportMailBtn: $('supportMailBtn'),
    supportIssueBtn: $('supportIssueBtn'),
    licenseInput: $('licenseInput'),
    licenseApplyBtn: $('licenseApplyBtn'),
    licenseRemoveBtn: $('licenseRemoveBtn'),
    proMsg: $('proMsg'),
    clipBadge: $('clipBadge'),
    clipMeta: $('clipMeta'),
    clipText: $('clipText'),
    clipImageWrap: $('clipImageWrap'),
    clipImage: $('clipImage'),
    pcSendText: $('pcSendText'),
    pcSendBtn: $('pcSendBtn'),
    pcSendMsg: $('pcSendMsg'),
    toast: $('toast'),
  };
  let lastClipHash = null;
  let clipFirstLoad = true;
  let clipLoading = false;
  let currentClip = null;
  let imageSettingsDirty = false;   // 入力中は poll で上書きしない
  let hotkeysDirty = false;
  let hotkeyRowsBuilt = false;
  let historyTimer = null;
  let historyLoadedAt = 0;
  let lastPro = null;

  let lastPairUrl = null;
  let pollTimer = null;
  let toastTimer = null;
  let stopped = false;
  let lastStatus = null;
  let restarting = false;
  let restartAt = 0;
  let updateBusy = false;

  function toast(text, kind = '') {
    els.toast.textContent = text;
    els.toast.className = 'toast ' + kind;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 3000);
  }

  function relativeTime(epochSeconds, now) {
    const diff = Math.max(0, now - epochSeconds);
    if (diff < 5) return 'たった今';
    if (diff < 60) return `${Math.floor(diff)} 秒前`;
    if (diff < 3600) return `${Math.floor(diff / 60)} 分前`;
    const date = new Date(epochSeconds * 1000);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }

  function formatBytes(n) {
    if (!n) return '0 KB';
    return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;
  }

  function renderImages(status) {
    const rules = status.image_rules || {};
    if (!imageSettingsDirty) {
      els.imageDirInput.value = rules.dir || status.image_dir || '';
      els.imageSubfolder.value = rules.subfolder || 'none';
      els.imageTemplate.value = rules.template || rules.default_template || '';
    }
    els.imageExample.textContent = rules.example || '—';
    els.imageRulesLock.hidden = !!status.pro;
    const last = status.last_image;
    if (!last) { els.imageLast.textContent = ''; return; }
    const notes = [formatBytes(last.size), relativeTime(last.received, status.now)];
    if (!last.saved) notes.push('クリップボードのみ');
    if (!last.clipboard) notes.push('クリップボードには入れられませんでした');
    els.imageLast.textContent = `最後に受け取った画像: ${last.name}（${notes.join(' ・ ')}）`;
  }

  // ---- PC のクリップボード（iPhone 画面と同じカード） ----------------------------
  function clipMetaText(clip, now) {
    const when = relativeTime(clip.updated, now);
    const who = clip.source === 'phone' ? 'iPhone から' : 'PC でコピー';
    if (clip.kind === 'image') {
      const image = clip.image;
      const dims = image && image.width && image.height ? `${image.width}×${image.height} ・ ` : '';
      return `画像 ${dims}${image ? formatBytes(image.size) + ' ・ ' : ''}${who} ・ ${when}`;
    }
    if (clip.kind === 'text') return `${clip.length.toLocaleString()} 文字 ・ ${who} ・ ${when}`;
    return when;
  }

  function renderClip(clip, now) {
    const changed = clip.hash !== lastClipHash;
    lastClipHash = clip.hash;
    currentClip = clip;
    const hasImage = clip.kind === 'image' && !!clip.image;
    els.clipText.hidden = hasImage;
    els.clipImageWrap.hidden = !hasImage;
    if (clip.kind === 'text' && clip.text) {
      els.clipText.textContent = clip.text;
      els.clipText.classList.remove('empty');
    } else if (clip.kind === 'image') {
      els.clipText.textContent = '（画像が入っていますが、表示できる形式ではありません）';
      els.clipText.classList.add('empty');
      if (hasImage) els.clipImage.src = `/pc/api/clip/image?id=${encodeURIComponent(clip.image.id)}`;
    } else {
      els.clipText.textContent = clip.kind === 'other' ? '（文字でも画像でもない内容です）' : '（空です）';
      els.clipText.classList.add('empty');
    }
    if (!hasImage) els.clipImage.removeAttribute('src');
    els.clipMeta.textContent = clipMetaText(clip, now);
    if (changed && !clipFirstLoad) {
      els.clipBadge.hidden = clip.source !== 'phone';
      const target = hasImage ? els.clipImageWrap : els.clipText;
      target.classList.remove('flash');
      void target.offsetWidth;
      target.classList.add('flash');
    }
    clipFirstLoad = false;
  }

  async function loadClip(status) {
    if (clipLoading) return;
    clipLoading = true;
    try {
      const response = await fetch('/pc/api/clip', { cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      renderClip(await response.json(), status.now);
    } catch {
      /* 次の poll で再試行 */
    } finally {
      clipLoading = false;
    }
  }

  els.pcSendBtn.addEventListener('click', async () => {
    const text = els.pcSendText.value;
    if (!text.trim()) { els.pcSendMsg.textContent = '送る文字を入力してください。'; els.pcSendMsg.className = 'msg error'; return; }
    els.pcSendBtn.disabled = true;
    try {
      const result = await adminPost('clip', { text });
      els.pcSendText.value = '';
      els.pcSendMsg.textContent = `iPhone へ送りました（${result.length.toLocaleString()} 文字）。iPhone の画面を開いていれば「新着」として届きます。`;
      els.pcSendMsg.className = 'msg ok';
      poll();
    } catch (error) {
      els.pcSendMsg.textContent = error.message;
      els.pcSendMsg.className = 'msg error';
    } finally {
      els.pcSendBtn.disabled = false;
    }
  });
  els.pcSendText.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') els.pcSendBtn.click();
  });

  // ---- Pro: ライセンス -----------------------------------------------------------
  function renderPro(status) {
    const license = status.license || {};
    const pro = !!status.pro;
    els.proPill.className = 'pill ' + (pro ? 'ok' : '');
    els.proPill.textContent = pro ? 'Pro 有効' : '無料版';
    els.proFeatures.querySelectorAll('[data-feature]').forEach((icon) => { icon.textContent = pro ? '✅' : '🔒'; });
    els.proApply.hidden = pro;
    els.proInfo.hidden = !pro;
    if (pro) {
      const who = [license.name, license.email].filter(Boolean).join(' ・ ');
      els.proHolder.textContent = `ライセンス ID: ${license.id || '—'}${who ? ` ・ ${who}` : ''}${license.expires ? ` ・ 有効期限 ${license.expires}` : ' ・ 無期限'}`;
      const subject = encodeURIComponent(`[ClipBridge Pro] サポート依頼 (${license.id || ''})`);
      const bodyText = encodeURIComponent(`ライセンス ID: ${license.id || ''}\nバージョン: ${status.version}\nPC: ${status.pc_name}\n\n困っていること:\n`);
      els.supportMailBtn.hidden = !license.support_email;
      if (license.support_email) els.supportMailBtn.href = `mailto:${license.support_email}?subject=${subject}&body=${bodyText}`;
      els.supportIssueBtn.href = `${license.support_url}&title=${subject}&body=${bodyText}`;
      els.proLead.textContent = 'Pro の機能がすべて有効です。同じキーをほかの PC でも使えます。';
    } else {
      els.proLead.textContent = license.error && license.error !== 'ライセンスキーが入力されていません'
        ? `保存されているキーは使えません: ${license.error}`
        : '1 人分のライセンスで、お持ちの PC 何台でも使えます。キーを貼り付けて適用してください。';
    }
    if (lastPro !== pro) {
      lastPro = pro;
      if (pro) loadHistory();
    }
  }

  els.licenseApplyBtn.addEventListener('click', async () => {
    const key = els.licenseInput.value.trim();
    if (!key) { els.proMsg.textContent = 'ライセンスキーを貼り付けてください。'; els.proMsg.className = 'msg error'; return; }
    els.licenseApplyBtn.disabled = true;
    try {
      const result = await adminPost('license', { key });
      els.licenseInput.value = '';
      els.proMsg.textContent = `Pro を有効にしました（${result.license.id}）`;
      els.proMsg.className = 'msg ok';
      toast('ClipBridge Pro が有効になりました', 'ok');
      poll();
    } catch (error) {
      els.proMsg.textContent = error.message;
      els.proMsg.className = 'msg error';
    } finally {
      els.licenseApplyBtn.disabled = false;
    }
  });

  els.licenseRemoveBtn.addEventListener('click', async () => {
    if (!window.confirm('この PC からライセンスを外します。履歴やホットキーなどの Pro 機能は使えなくなります（キーを再入力すれば戻せます）。よろしいですか？')) return;
    try {
      await adminPost('license/remove');
      els.proMsg.textContent = 'ライセンスを外しました。';
      els.proMsg.className = 'msg';
      poll();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  // ---- Pro: 画像の保存ルール ---------------------------------------------------
  for (const input of [els.imageDirInput, els.imageSubfolder, els.imageTemplate]) {
    input.addEventListener('input', () => { imageSettingsDirty = true; });
    input.addEventListener('change', () => { imageSettingsDirty = true; });
  }
  els.imageSettingsBtn.addEventListener('click', async () => {
    const body = { dir: els.imageDirInput.value };
    if (lastStatus && lastStatus.pro) {
      body.subfolder = els.imageSubfolder.value;
      body.template = els.imageTemplate.value;
    }
    els.imageSettingsBtn.disabled = true;
    try {
      const result = await adminPost('images/settings', body);
      imageSettingsDirty = false;
      els.imageSettingsMsg.textContent = `保存しました。例: ${result.example}`;
      els.imageSettingsMsg.className = 'msg ok';
      poll();
    } catch (error) {
      els.imageSettingsMsg.textContent = error.message;
      els.imageSettingsMsg.className = 'msg error';
    } finally {
      els.imageSettingsBtn.disabled = false;
    }
  });

  // ---- Pro: 履歴 -----------------------------------------------------------------
  function historyItem(entry, now) {
    const li = document.createElement('li');
    li.className = 'history-item' + (entry.pinned ? ' pinned' : '');
    if (entry.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'history-thumb';
      img.alt = `画像 ${entry.width}×${entry.height}`;
      img.loading = 'lazy';
      img.src = `/pc/api/history/${entry.id}/image`;
      li.append(img);
    } else {
      const text = document.createElement('pre');
      text.className = 'history-text';
      text.textContent = entry.text + (entry.truncated ? ' …' : '');
      text.addEventListener('click', () => text.classList.toggle('expanded'));
      li.append(text);
    }
    const meta = document.createElement('div');
    meta.className = 'history-meta';
    meta.textContent = [
      entry.source === 'phone' ? 'iPhone から' : 'PC でコピー',
      relativeTime(entry.created, now),
      entry.kind === 'text' ? `${entry.length.toLocaleString()} 文字` : '',
      entry.pinned ? 'ピン留め' : '',
    ].filter(Boolean).join(' ・ ');
    const actions = document.createElement('div');
    actions.className = 'history-actions';
    const restore = document.createElement('button');
    restore.type = 'button';
    restore.className = 'btn small primary';
    restore.textContent = 'クリップボードに戻す';
    restore.addEventListener('click', async () => {
      try { await adminPost('history/restore', { id: entry.id }); toast('クリップボードに戻しました'); } catch (error) { toast(error.message, 'error'); }
    });
    const pin = document.createElement('button');
    pin.type = 'button';
    pin.className = 'btn small' + (entry.pinned ? ' on' : '');
    pin.textContent = entry.pinned ? 'ピン留め解除' : 'ピン留め';
    pin.addEventListener('click', async () => {
      try { await adminPost('history/pin', { id: entry.id, pinned: !entry.pinned }); loadHistory(true); } catch (error) { toast(error.message, 'error'); }
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn small danger';
    remove.textContent = '削除';
    remove.addEventListener('click', async () => {
      try { await adminPost('history/delete', { id: entry.id }); loadHistory(true); } catch (error) { toast(error.message, 'error'); }
    });
    actions.append(restore, pin, remove);
    li.append(meta, actions);
    return li;
  }

  async function loadHistory(force = false) {
    if (!lastStatus || !lastStatus.pro) return;
    if (!force && Date.now() - historyLoadedAt < 1500) return;
    historyLoadedAt = Date.now();
    try {
      const response = await fetch('/pc/api/history?q=' + encodeURIComponent(els.historySearch.value.trim()), { cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      els.historyList.innerHTML = '';
      if (!data.items.length) {
        const empty = document.createElement('li');
        empty.className = 'history-empty';
        empty.textContent = els.historySearch.value ? '一致する項目がありません。' : 'まだ履歴がありません。PC でコピーするか、iPhone から送ると記録されます。';
        els.historyList.appendChild(empty);
      }
      for (const entry of data.items) els.historyList.appendChild(historyItem(entry, lastStatus.now));
      els.historyCount.textContent = `${data.total} 件（ピン留め ${data.pinned}）`;
    } catch {
      els.historyMsg.textContent = '履歴を読み込めませんでした。';
      els.historyMsg.className = 'msg error';
    }
  }

  function renderHistory(status) {
    const pro = !!status.pro;
    els.historyLock.hidden = pro;
    els.historyBody.hidden = !pro;
    if (!pro) { els.historyCount.textContent = ''; return; }
    els.historyToggle.checked = !!(status.history && status.history.enabled);
    if (status.history && status.history.total !== undefined) {
      const shown = els.historyList.querySelectorAll('.history-item').length;
      if (status.history.total !== shown && !els.historySearch.value) loadHistory();
    }
  }

  els.historySearch.addEventListener('input', () => {
    clearTimeout(historyTimer);
    historyTimer = setTimeout(() => loadHistory(true), 300);
  });
  els.historyToggle.addEventListener('change', async () => {
    try { await adminPost('history/settings', { enabled: els.historyToggle.checked }); toast(els.historyToggle.checked ? '履歴を記録します' : '履歴の記録を止めました'); }
    catch (error) { els.historyToggle.checked = !els.historyToggle.checked; toast(error.message, 'error'); }
  });
  els.historyClearBtn.addEventListener('click', async () => {
    if (!window.confirm('ピン留め以外の履歴をすべて消去します。よろしいですか？')) return;
    try { const result = await adminPost('history/clear'); toast(`${result.removed} 件を消去しました`); loadHistory(true); } catch (error) { toast(error.message, 'error'); }
  });

  // ---- Pro: ホットキー ------------------------------------------------------------
  function renderHotkeys(status) {
    const hotkeys = status.hotkeys || {};
    const pro = !!status.pro;
    els.hotkeyLock.hidden = pro;
    els.hotkeyBody.hidden = !pro;
    if (!hotkeys.supported) {
      els.hotkeyState.textContent = 'この OS では使えません';
    } else if (!pro) {
      els.hotkeyState.textContent = '';
    } else {
      els.hotkeyState.textContent = hotkeys.enabled ? (hotkeys.active ? '有効' : '登録できませんでした') : '無効';
    }
    if (!pro) return;
    if (!hotkeyRowsBuilt) {
      els.hotkeyRows.innerHTML = '';
      for (const [action, label] of Object.entries(hotkeys.actions || {})) {
        const name = document.createElement('label');
        name.className = 'label';
        name.htmlFor = `hotkey-${action}`;
        name.textContent = label;
        const input = document.createElement('input');
        input.className = 'text-input';
        input.id = `hotkey-${action}`;
        input.dataset.action = action;
        input.spellcheck = false;
        input.addEventListener('input', () => { hotkeysDirty = true; });
        els.hotkeyRows.append(name, input);
      }
      hotkeyRowsBuilt = true;
    }
    if (!hotkeysDirty) {
      els.hotkeyToggle.checked = !!hotkeys.enabled;
      for (const input of els.hotkeyRows.querySelectorAll('input')) input.value = (hotkeys.bindings || {})[input.dataset.action] || '';
    }
    const errors = Object.entries(hotkeys.errors || {});
    if (errors.length && hotkeys.enabled) {
      els.hotkeyMsg.textContent = errors.map(([action, message]) => `${(hotkeys.actions || {})[action] || action}: ${message}`).join(' / ');
      els.hotkeyMsg.className = 'msg error';
    } else if (els.hotkeyMsg.className.includes('error')) {
      els.hotkeyMsg.textContent = '';
      els.hotkeyMsg.className = 'msg';
    }
  }

  els.hotkeyToggle.addEventListener('change', () => { hotkeysDirty = true; });
  els.hotkeySaveBtn.addEventListener('click', async () => {
    const bindings = {};
    for (const input of els.hotkeyRows.querySelectorAll('input')) bindings[input.dataset.action] = input.value.trim();
    els.hotkeySaveBtn.disabled = true;
    try {
      const result = await adminPost('hotkeys', { enabled: els.hotkeyToggle.checked, bindings });
      hotkeysDirty = false;
      const errors = Object.entries(result.errors || {});
      els.hotkeyMsg.textContent = errors.length ? '一部のキーを登録できませんでした。' : '保存しました。';
      els.hotkeyMsg.className = 'msg ' + (errors.length ? 'error' : 'ok');
      poll();
    } catch (error) {
      els.hotkeyMsg.textContent = error.message;
      els.hotkeyMsg.className = 'msg error';
    } finally {
      els.hotkeySaveBtn.disabled = false;
    }
  });

  async function adminPost(action, body = {}) {
    const response = await fetch('/pc/api/' + action, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-ClipBridge': '1' },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || `エラー (${response.status})`);
    return data;
  }

  const ICONS = { ok: '✅', warn: '⚠️', fail: '❌' };

  function renderChecks(tailscale) {
    els.checks.innerHTML = '';
    for (const check of tailscale.checks) {
      const li = document.createElement('li');
      const icon = document.createElement('span');
      icon.className = 'icon';
      icon.textContent = ICONS[check.state] || '•';
      const body = document.createElement('div');
      const label = document.createElement('div');
      label.textContent = check.label;
      body.appendChild(label);
      if (check.detail) {
        const detail = document.createElement('div');
        detail.className = 'detail';
        detail.textContent = check.detail;
        body.appendChild(detail);
      }
      if (check.state !== 'ok' && check.hint) {
        const fix = document.createElement('div');
        fix.className = 'fix';
        fix.textContent = check.hint;
        body.appendChild(fix);
      }
      li.append(icon, body);
      els.checks.appendChild(li);
    }
    els.serveBtn.hidden = !(tailscale.serve_state === 'free' && tailscale.backend_state === 'Running');
  }

  function renderQr(pairUrl) {
    if (pairUrl === lastPairUrl) return;
    lastPairUrl = pairUrl;
    if (!pairUrl) {
      els.qr.innerHTML = '<div class="placeholder">手順 1 が完了すると QR コードが表示されます</div>';
      return;
    }
    try {
      const qr = window.qrcode(0, 'M');
      qr.addData(pairUrl);
      qr.make();
      els.qr.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
    } catch {
      els.qr.innerHTML = '<div class="placeholder">QR を生成できませんでした。URL とコードで接続してください。</div>';
    }
  }

  function renderDevices(devices, now) {
    els.devices.innerHTML = '';
    if (!devices.length) {
      const waiting = document.createElement('p');
      waiting.className = 'waiting';
      waiting.textContent = 'iPhone からの接続を待っています…';
      els.devices.appendChild(waiting);
      return;
    }
    for (const device of devices) {
      const row = document.createElement('div');
      row.className = 'device';
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = `✅ ${device.name} が接続しました`;
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = [device.user, device.ip].filter(Boolean).join(' ・ ');
      const when = document.createElement('span');
      when.className = 'when';
      when.textContent = relativeTime(device.last_seen, now);
      row.append(who, detail, when);
      els.devices.appendChild(row);
    }
  }

  function renderUpdate(status) {
    const update = status.update || {};
    els.updateVersion.textContent = `現在 ${update.current || status.version}`;
    let text = '';
    let cls = 'msg';
    if (restarting) {
      text = '新しいバージョンで再起動しています…';
    } else if (updateBusy) {
      text = '更新を取得しています…';
    } else if (update.error) {
      text = `更新情報を取得できませんでした: ${update.error}`;
      cls = 'msg error';
    } else if (!update.checked_at) {
      text = '更新を確認しています…';
    } else if (update.available) {
      text = update.method === 'git'
        ? `新しい変更があります（${update.detail}）: ${update.latest}`
        : `新しいバージョン ${update.latest} があります（${update.detail}）`;
      cls = 'msg ok';
    } else {
      text = `最新です（${relativeTime(update.checked_at, status.now)} に確認）`;
    }
    els.updateMsg.textContent = text;
    els.updateMsg.className = cls;
    els.updateApplyBtn.hidden = !(update.available && update.can_apply) || restarting;
    els.updateApplyBtn.disabled = updateBusy;
    els.updateCheckBtn.disabled = updateBusy || restarting;
    els.updateLink.hidden = !(update.available && !update.can_apply);
    if (update.releases_url) els.updateLink.href = update.releases_url;
    if (update.method === 'exe') {
      els.updateHint.textContent = `GitHub Releases から取得し、サイズと SHA-256 を検証してから入れ替えます。保存先: ${status.install_path || ''}`;
    } else if (update.method === 'git') {
      els.updateHint.textContent = 'git pull で更新して再起動します（ローカルに未コミットの変更があると中止します）。';
    } else {
      els.updateHint.textContent = 'ソース版です。配布ページから新しい版を取得するか、git clone したフォルダーから起動するとここから更新できます。';
    }
  }

  function render(status) {
    lastStatus = status;
    if (restarting && status.started > restartAt) {
      restarting = false;
      toast(`アップデートが完了しました（${status.version}）`, 'ok');
    }
    renderChecks(status.tailscale);
    renderUpdate(status);
    renderImages(status);
    renderPro(status);
    renderHistory(status);
    renderHotkeys(status);
    if (status.clip && status.clip.hash !== lastClipHash) loadClip(status);
    else if (currentClip) els.clipMeta.textContent = clipMetaText(currentClip, status.now);
    els.publicUrl.textContent = status.public_url || status.planned_url || '—';
    els.pairCode.textContent = status.pair_code;
    renderQr(status.pair_url);
    renderDevices(status.devices, status.now);
    els.startupToggle.checked = status.startup.enabled;
    els.startupToggle.disabled = !status.startup.supported;
    els.startupMsg.textContent = status.startup.supported
      ? (status.startup.enabled ? `登録済み: ${status.startup.path}` : 'スタートアップフォルダーに起動スクリプトを置きます（管理者権限は不要）。')
      : 'この OS では自動起動の登録に対応していません。';
    els.configPath.textContent = status.config_path;
    const backend = status.clipboard_backend === 'windows' ? 'Windows クリップボード' : '仮クリップボード（開発用）';
    els.infoLine.textContent = `${status.app} ${status.version} ・ ${status.pc_name} ・ 127.0.0.1:${status.port} ・ ${backend} ・ iPhone 接続中 ${status.subscribers} 台`;
    if (status.serve_result && !status.serve_result.ok && !els.serveMsg.textContent) {
      els.serveMsg.textContent = status.serve_result.message;
      els.serveMsg.className = 'msg error';
    }
  }

  async function poll() {
    if (stopped) return;
    try {
      const response = await fetch('/pc/api/status', { cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      render(await response.json());
      els.serverPill.className = 'pill ok';
      els.serverPill.textContent = 'サーバー稼働中';
    } catch {
      els.serverPill.className = restarting ? 'pill warn' : 'pill err';
      els.serverPill.textContent = restarting ? '再起動しています…' : 'サーバーに接続できません';
    }
  }

  els.serveBtn.addEventListener('click', async () => {
    els.serveBtn.disabled = true;
    els.serveMsg.textContent = 'Tailscale Serve を設定しています…';
    els.serveMsg.className = 'msg';
    try {
      const result = await adminPost('serve');
      els.serveMsg.textContent = result.message;
      els.serveMsg.className = 'msg ' + (result.ok ? 'ok' : 'error');
    } catch (error) {
      els.serveMsg.textContent = error.message;
      els.serveMsg.className = 'msg error';
    } finally {
      els.serveBtn.disabled = false;
      poll();
    }
  });

  els.recheckBtn.addEventListener('click', async () => {
    els.serveMsg.textContent = '';
    els.serveMsg.className = 'msg';
    try { await adminPost('serve'); } catch { /* 結果は poll で表示 */ }
    poll();
  });

  els.newCodeBtn.addEventListener('click', async () => {
    try {
      const result = await adminPost('pair-code');
      els.pairCode.textContent = result.pair_code;
      toast('新しいコードを発行しました');
      poll();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.startupToggle.addEventListener('change', async () => {
    const enabled = els.startupToggle.checked;
    els.startupToggle.disabled = true;
    try {
      await adminPost('startup', { enabled });
      toast(enabled ? '自動起動を登録しました' : '自動起動を解除しました');
    } catch (error) {
      els.startupToggle.checked = !enabled;
      toast(error.message, 'error');
    } finally {
      els.startupToggle.disabled = false;
      poll();
    }
  });

  els.rotateBtn.addEventListener('click', async () => {
    if (!window.confirm('接続中の iPhone はすべて切断され、もう一度コードで接続し直す必要があります。実行しますか？')) return;
    try {
      await adminPost('token/rotate');
      toast('すべての接続を解除しました。iPhone で新しいコードを入力してください。');
      poll();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.stopBtn.addEventListener('click', async () => {
    if (!window.confirm('ClipBridge サーバーを停止します。iPhone からは使えなくなります。よろしいですか？')) return;
    try {
      await adminPost('shutdown');
      stopped = true;
      clearInterval(pollTimer);
      els.serverPill.className = 'pill err';
      els.serverPill.textContent = '停止しました';
      toast(lastStatus && lastStatus.frozen
        ? 'サーバーを停止しました。再開するにはデスクトップの ClipBridge を開いてください。'
        : 'サーバーを停止しました。再開するには ClipBridge.bat を実行してください。');
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.openImagesBtn.addEventListener('click', async () => {
    try {
      const result = await adminPost('images/open');
      toast(`フォルダーを開きました: ${result.path}`);
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.updateCheckBtn.addEventListener('click', async () => {
    updateBusy = true;
    if (lastStatus) renderUpdate(lastStatus);
    try {
      const info = await adminPost('update/check');
      if (lastStatus) lastStatus.update = info;
      if (info.error) toast('更新情報を取得できませんでした', 'error');
      else toast(info.available ? '新しいバージョンがあります' : '最新です');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      updateBusy = false;
      if (lastStatus) renderUpdate(lastStatus);
    }
  });

  els.updateApplyBtn.addEventListener('click', async () => {
    const update = (lastStatus && lastStatus.update) || {};
    const what = update.method === 'git' ? '最新の変更' : `バージョン ${update.latest}`;
    if (!window.confirm(`${what} を適用して ClipBridge を再起動します。iPhone との接続は数秒間切れますが、設定は引き継がれます。続けますか？`)) return;
    updateBusy = true;
    if (lastStatus) renderUpdate(lastStatus);
    try {
      const result = await adminPost('update/apply');
      restarting = true;
      restartAt = Date.now() / 1000;
      toast(result.message);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      updateBusy = false;
      if (lastStatus) renderUpdate(lastStatus);
    }
  });

  poll();
  pollTimer = setInterval(poll, 2000);
})();
