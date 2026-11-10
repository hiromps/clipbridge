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
    imageDir: $('imageDir'),
    openImagesBtn: $('openImagesBtn'),
    toast: $('toast'),
  };

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
    els.imageDir.textContent = status.image_dir || '—';
    const last = status.last_image;
    if (!last) { els.imageLast.textContent = ''; return; }
    const notes = [formatBytes(last.size), relativeTime(last.received, status.now)];
    if (!last.saved) notes.push('クリップボードのみ');
    if (!last.clipboard) notes.push('クリップボードには入れられませんでした');
    els.imageLast.textContent = `最後に受け取った画像: ${last.name}（${notes.join(' ・ ')}）`;
  }

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
