(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { token: '', online: false, uploads: new Map(), pendingDelete: null, pollTimer: null, reconnectTimer: null, refreshing: false };
  const els = {
    authPanel: $('authPanel'), authForm: $('authForm'), tokenInput: $('tokenInput'), authError: $('authError'), workspace: $('workspace'),
    connectionBadge: $('connectionBadge'), connectionText: $('connectionText'), fileInput: $('fileInput'), chooseFiles: $('chooseFiles'), uploadQueue: $('uploadQueue'),
    textInput: $('textInput'), textCount: $('textCount'), copyText: $('copyText'), sendText: $('sendText'), messages: $('messages'),
    filesSummary: $('filesSummary'), filesList: $('filesList'), refreshFiles: $('refreshFiles'), textHistory: $('textHistory'), refreshText: $('refreshText'),
    toastRegion: $('toastRegion'), deleteDialog: $('deleteDialog'), deletePrompt: $('deletePrompt'), confirmDelete: $('confirmDelete'), disconnect: $('disconnect')
  };

  function apiUrl(path) { return new URL(path, window.location.href).toString(); }
  function authHeaders(extra = {}) { return { ...extra, Authorization: `Bearer ${state.token}` }; }
  function errorMessage(payload, fallback) { return payload?.error?.message || payload?.detail?.message || payload?.message || fallback; }
  async function request(path, options = {}) {
    const response = await fetch(apiUrl(path), { ...options, headers: authHeaders(options.headers || {}), cache: 'no-store' });
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch (_) {}
      const error = new Error(errorMessage(payload, `Ошибка запроса (${response.status})`));
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }
  function setConnection(online, label) {
    state.online = online;
    els.connectionBadge.classList.toggle('online', online);
    els.connectionBadge.classList.toggle('offline', !online);
    els.connectionBadge.classList.remove('pending');
    els.connectionText.textContent = label || (online ? 'На связи' : 'Нет связи');
  }
  function toast(message, type = '') {
    const node = document.createElement('div'); node.className = `toast ${type}`.trim(); node.textContent = message;
    els.toastRegion.append(node); window.setTimeout(() => node.remove(), 4200);
  }
  function addMessage(message, type = '') {
    const node = document.createElement('div'); node.className = `message-line ${type}`.trim(); node.textContent = message;
    els.messages.prepend(node); while (els.messages.children.length > 5) els.messages.lastElementChild.remove();
  }
  function fmtBytes(bytes) {
    if (bytes === 0) return '0 Б';
    if (!Number.isFinite(bytes) || bytes < 0) return '—';
    const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ']; const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** index)).toLocaleString('ru-RU', { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
  }
  function fmtDate(value) {
    const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  }
  function escapeText(value) { return String(value ?? ''); }
  function node(tag, className, text) { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = text; return n; }
  function clearChildren(element) { element.replaceChildren(); }

  async function connect(token) {
    state.token = token.trim();
    if (!state.token) return;
    try {
      await request('/api/files');
      sessionStorage.setItem('localTransferToken', state.token);
      els.authError.hidden = true; els.authPanel.hidden = true; els.workspace.hidden = false;
      setConnection(true, 'На связи');
      await Promise.all([refreshFiles(), refreshTextHistory()]);
      startPolling();
    } catch (error) {
      state.token = ''; try { sessionStorage.removeItem('localTransferToken'); } catch (_) {}
      els.authError.textContent = error.message; els.authError.hidden = false;
      els.tokenInput.focus(); setConnection(false, error.status === 401 ? 'Неверный токен' : 'Нет связи');
    }
  }
  function disconnect() {
    stopPolling(); state.token = ''; state.online = false;
    try { sessionStorage.removeItem('localTransferToken'); } catch (_) {}
    els.workspace.hidden = true; els.authPanel.hidden = false; els.tokenInput.value = ''; els.authError.hidden = true;
    setConnection(false, 'Отключено');
  }
  function startPolling() {
    stopPolling(); state.pollTimer = window.setInterval(async () => {
      if (document.visibilityState !== 'visible' || state.refreshing) return;
      try { await request('/api/health'); setConnection(true, 'На связи'); }
      catch (_) { setConnection(false, 'Нет связи'); }
      if (state.online) { refreshFiles().catch(() => {}); refreshTextHistory().catch(() => {}); }
    }, 12000);
  }
  function stopPolling() { if (state.pollTimer) window.clearInterval(state.pollTimer); state.pollTimer = null; }

  function makeUploadItem(file) {
    const wrap = node('article', 'queue-item');
    const top = node('div', 'queue-top'); const icon = node('div', 'file-symbol', '↑'); icon.setAttribute('aria-hidden', 'true');
    const info = node('div', 'queue-info'); const name = node('div', 'queue-name', file.name); const meta = node('div', 'queue-meta', `${fmtBytes(file.size)} · ожидает отправки`); info.append(name, meta);
    const status = node('div', 'queue-status', 'В очереди'); top.append(icon, info, status);
    const track = node('div', 'progress-track'); const bar = node('div', 'progress-bar'); track.append(bar);
    const progress = node('div', 'progress-meta'); const percent = node('span', '', '0%'); const detail = node('span', '', `0 Б из ${fmtBytes(file.size)}`); progress.append(percent, detail);
    const controls = node('div', 'queue-controls'); const cancel = node('button', 'button secondary', 'Отменить'); cancel.type = 'button';
    controls.append(cancel); wrap.append(top, track, progress, controls);
    const id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const record = { id, file, wrap, name, meta, status, bar, percent, detail, cancel, xhr: null, cancelled: false, done: false };
    cancel.addEventListener('click', () => {
      record.cancelled = true;
      if (record.xhr) record.xhr.abort();
      else { record.done = true; status.textContent = 'Отменено'; status.className = 'queue-status error'; meta.textContent = `${fmtBytes(file.size)} · передача отменена`; cancel.disabled = true; }
    });
    state.uploads.set(id, record); els.uploadQueue.prepend(wrap); return record;
  }
  function updateProgress(record, loaded, total, startedAt) {
    const safeLoaded = Math.max(0, Math.min(loaded, total));
    const pct = total > 0 ? Math.floor((safeLoaded / total) * 100) : (record.file.size === 0 && safeLoaded === 0 ? 0 : 0);
    record.bar.style.width = `${pct}%`; record.percent.textContent = `${pct}%`;
    record.detail.textContent = `${fmtBytes(safeLoaded)} из ${fmtBytes(total)}`;
    const seconds = Math.max((performance.now() - startedAt) / 1000, 0.001); const speed = safeLoaded / seconds;
    record.meta.textContent = `${fmtBytes(record.file.size)} · ${fmtBytes(speed)}/с`;
  }
  function uploadFile(record) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest(); record.xhr = xhr;
      const startedAt = performance.now(); let lastLoaded = 0;
      record.status.textContent = 'Подготовка…'; record.status.className = 'queue-status active'; record.cancel.disabled = false;
      xhr.open('POST', apiUrl(`/api/files?filename=${encodeURIComponent(record.file.name)}`));
      xhr.setRequestHeader('Authorization', `Bearer ${state.token}`); xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', (event) => {
        if (!event.lengthComputable) {
          const loaded = Math.min(event.loaded, record.file.size); lastLoaded = loaded;
          record.detail.textContent = `${fmtBytes(loaded)} передано`; record.meta.textContent = `${fmtBytes(record.file.size)} · вычисление процента недоступно`;
          return;
        }
        lastLoaded = event.loaded; updateProgress(record, event.loaded, event.total, startedAt);
      });
      xhr.addEventListener('load', () => {
        record.xhr = null; record.done = true; record.cancel.disabled = true;
        if (xhr.status >= 200 && xhr.status < 300) {
          record.bar.style.width = '100%'; record.percent.textContent = '100%'; record.detail.textContent = `${fmtBytes(record.file.size)} передано`;
          record.status.textContent = 'Сохранён'; record.status.className = 'queue-status success'; record.meta.textContent = `${fmtBytes(record.file.size)} · успешно`; resolve(true);
        } else {
          let message = `Ошибка сервера (${xhr.status})`; try { message = errorMessage(JSON.parse(xhr.responseText), message); } catch (_) {}
          record.status.textContent = 'Ошибка'; record.status.className = 'queue-status error'; record.meta.textContent = message; record.detail.textContent = `${fmtBytes(lastLoaded)} передано; сохранение не подтверждено`;
          addMessage(`${record.file.name}: ${message}`, 'error'); resolve(false);
        }
      });
      xhr.addEventListener('error', () => {
        record.xhr = null; record.done = true; record.status.textContent = record.cancelled ? 'Отменено' : 'Нет связи'; record.status.className = 'queue-status error';
        record.meta.textContent = record.cancelled ? 'Передача отменена; файл не считается сохранённым' : 'Соединение прервано. Проверьте список перед повторной отправкой.';
        record.detail.textContent = `${fmtBytes(lastLoaded)} передано; итог неизвестен`; record.cancel.disabled = true;
        if (!record.cancelled) addMessage(`${record.file.name}: соединение прервано; проверьте список сервера перед повтором`, 'error'); resolve(false);
      });
      xhr.addEventListener('abort', () => {
        record.xhr = null; record.done = true; record.status.textContent = 'Отменено'; record.status.className = 'queue-status error';
        record.meta.textContent = 'Передача отменена; файл не считается сохранённым'; record.detail.textContent = `${fmtBytes(lastLoaded)} передано`; record.cancel.disabled = true; resolve(false);
      });
      record.status.textContent = 'Отправка…'; xhr.send(record.file);
    });
  }
  async function uploadFiles(files) {
    const selected = Array.from(files || []); if (!selected.length) return;
    const queue = selected.map(makeUploadItem); els.fileInput.value = '';
    // Sequential uploads keep memory/network pressure predictable and make cancellation unambiguous.
    for (const record of queue) {
      if (record.cancelled) continue;
      if (!state.online) { record.status.textContent = 'Ожидает связи'; record.status.className = 'queue-status error'; continue; }
      await uploadFile(record);
    }
    try { await refreshFiles(); } catch (_) { setConnection(false, 'Нет связи'); }
  }

  async function refreshFiles() {
    if (!state.token) return;
    const data = await request('/api/files'); const items = Array.isArray(data.items) ? data.items : [];
    clearChildren(els.filesList); els.filesSummary.textContent = `${items.length} ${plural(items.length, 'файл', 'файла', 'файлов')}`;
    if (!items.length) { els.filesList.append(node('div', 'empty-state', 'Пока нет сохранённых файлов. Выберите файл выше, чтобы отправить его с телефона.')); return; }
    for (const item of items) {
      const row = node('article', 'file-row'); const icon = node('div', 'file-symbol', '↓'); icon.setAttribute('aria-hidden', 'true');
      const main = node('div', 'file-main'); main.append(node('div', 'file-name', escapeText(item.name)), node('div', 'file-details', `${fmtBytes(item.size)}${item.created_at ? ` · ${fmtDate(item.created_at)}` : ''}`));
      const actions = node('div', 'file-actions'); const download = node('button', 'icon-button', '↓'); download.type = 'button'; download.title = `Скачать ${item.name}`; download.setAttribute('aria-label', `Скачать ${item.name}`); download.addEventListener('click', () => downloadFile(item));
      const del = node('button', 'icon-button delete', '×'); del.type = 'button'; del.title = `Удалить ${item.name}`; del.setAttribute('aria-label', `Удалить ${item.name}`); del.addEventListener('click', () => askDelete(item));
      actions.append(download, del); row.append(icon, main, actions); els.filesList.append(row);
    }
    setConnection(true, 'На связи');
  }
  function plural(n, one, few, many) { const n10=n%10,n100=n%100; return n10===1&&n100!==11?one:n10>=2&&n10<=4&&(n100<12||n100>14)?few:many; }
  async function downloadFile(item) {
    // Open a same-origin tab synchronously in the click gesture. This avoids
    // Safari/iOS treating the later, post-fetch navigation as a blocked popup.
    const downloadTab = window.open('about:blank', '_blank');
    try {
      const ticket = await request(`/api/files/${encodeURIComponent(item.id)}/download-ticket`, { method: 'POST' });
      const target = apiUrl(ticket.url);
      if (downloadTab && !downloadTab.closed) downloadTab.location.replace(target);
      else {
        // Fallback for browsers that disallow new tabs; direct navigation lets
        // the browser handle Content-Disposition without buffering a Blob.
        const link = document.createElement('a'); link.href = target; link.rel = 'noopener'; link.download = item.name; link.style.display = 'none'; document.body.append(link); link.click(); link.remove();
      }
      toast(`Скачивание «${item.name}» передано браузеру.`);
    } catch (error) {
      if (downloadTab && !downloadTab.closed) downloadTab.close();
      toast(`Не удалось начать скачивание: ${error.message}`, 'error'); if (error.status === 401) disconnect();
    }
  }
  function askDelete(item) {
    state.pendingDelete = item; els.deletePrompt.textContent = `Файл «${item.name}» (${fmtBytes(item.size)}) будет удалён с компьютера.`;
    if (typeof els.deleteDialog.showModal === 'function') els.deleteDialog.showModal();
    else if (window.confirm(els.deletePrompt.textContent)) performDelete();
  }
  async function performDelete() {
    const item = state.pendingDelete; if (!item) return;
    els.confirmDelete.disabled = true;
    try { await request(`/api/files/${encodeURIComponent(item.id)}`, { method: 'DELETE' }); toast(`Удалён файл «${item.name}».`); await refreshFiles(); }
    catch (error) { toast(`Не удалось удалить файл: ${error.message}`, 'error'); }
    finally { els.confirmDelete.disabled = false; state.pendingDelete = null; if (els.deleteDialog.open) els.deleteDialog.close(); }
  }
  async function sendText() {
    const text = els.textInput.value; if (!text.trim()) { addMessage('Введите текст перед отправкой.', 'error'); return; }
    els.sendText.disabled = true;
    try {
      await request('/api/text', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
      els.textInput.value = ''; updateTextCount(); addMessage('Текст успешно отправлен.', 'success'); await refreshTextHistory();
    } catch (error) { addMessage(`Текст не отправлен: ${error.message}. Текст оставлен в поле.`, 'error'); if (error.status === 401) disconnect(); }
    finally { els.sendText.disabled = false; }
  }
  async function copyText() {
    const text = els.textInput.value; if (!text) { addMessage('Поле пустое — нечего копировать.', 'error'); return; }
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else { els.textInput.focus(); els.textInput.select(); const ok = document.execCommand('copy'); if (!ok) throw new Error('Копирование не поддерживается'); }
      toast('Текст скопирован.');
    } catch (_) { els.textInput.focus(); els.textInput.select(); addMessage('Автоматическое копирование недоступно. Текст выделен — скопируйте его вручную.', 'error'); }
  }
  function updateTextCount() { els.textCount.textContent = `${els.textInput.value.length.toLocaleString('ru-RU')} символов`; }
  async function refreshTextHistory() {
    if (!state.token) return;
    const data = await request('/api/text?limit=100'); const items = Array.isArray(data.items) ? data.items : [];
    clearChildren(els.textHistory);
    if (!items.length) { els.textHistory.append(node('div', 'empty-state', 'Сообщений пока нет.')); return; }
    for (const item of [...items].reverse()) {
      const card = node('article', 'history-item'); const top = node('div', 'history-top'); top.append(node('span', 'queue-status', `Сообщение #${item.id}`), node('time', 'history-date', fmtDate(item.created_at)));
      const content = node('div', 'history-content', item.text); const actions = node('div', 'history-actions'); const copy = node('button', 'button secondary', 'Копировать'); copy.type = 'button'; copy.addEventListener('click', async () => {
        try { if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(item.text); else throw new Error(); toast('Сообщение скопировано.'); }
        catch (_) { const area = document.createElement('textarea'); area.value = item.text; area.style.position = 'fixed'; area.style.opacity = '0'; document.body.append(area); area.select(); const ok = document.execCommand('copy'); area.remove(); if (ok) toast('Сообщение скопировано.'); else toast('Скопируйте текст вручную.', 'error'); }
      }); actions.append(copy); card.append(top, content, actions); els.textHistory.append(card);
    }
  }

  els.authForm.addEventListener('submit', (event) => { event.preventDefault(); connect(els.tokenInput.value); });
  els.chooseFiles.addEventListener('click', () => els.fileInput.click());
  els.fileInput.addEventListener('change', () => uploadFiles(els.fileInput.files));
  els.textInput.addEventListener('input', updateTextCount);
  els.copyText.addEventListener('click', copyText); els.sendText.addEventListener('click', sendText);
  els.refreshFiles.addEventListener('click', () => refreshFiles().catch((error) => toast(`Не удалось обновить список: ${error.message}`, 'error')));
  els.refreshText.addEventListener('click', () => refreshTextHistory().catch((error) => toast(`Не удалось загрузить историю: ${error.message}`, 'error')));
  els.confirmDelete.addEventListener('click', (event) => { event.preventDefault(); performDelete(); });
  els.disconnect.addEventListener('click', disconnect);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && state.token) { refreshFiles().catch(() => setConnection(false, 'Нет связи')); refreshTextHistory().catch(() => {}); } });
  window.addEventListener('online', () => { if (state.token) { setConnection(true, 'Проверка связи'); refreshFiles().catch(() => setConnection(false, 'Нет связи')); } });
  window.addEventListener('offline', () => setConnection(false, 'Нет сети'));
  updateTextCount();
  try { const saved = sessionStorage.getItem('localTransferToken'); if (saved) connect(saved); } catch (_) {}
})();
