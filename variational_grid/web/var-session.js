(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dialog = $('var-session-dialog'), input = $('var-session-input'), save = $('var-session-save'), cancel = $('var-session-cancel');
  let csrf = '', sending = false, stateRequest = null;
  function message(text, error = false) {
    const target = $('var-session-result');
    target.textContent = text;
    target.hidden = !text;
    target.classList.toggle('error', error);
  }
  function stateText(state) {
    $('var-session-state').textContent = state.state === 'stored' && state.expires_utc
      ? `已保存会话 · 到期 ${new Date(state.expires_utc).toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'})}（北京时间）`
      : '当前会话缺失、过期或不可读取，请粘贴新 token。';
  }
  function buttons() {save.disabled = sending || !csrf || !input.value.trim(); cancel.disabled = sending; input.disabled = sending;}
  $('var-session-open').onclick = async () => {
    if (dialog.open) return;
    input.value = ''; csrf = ''; message(''); buttons();
    $('var-session-state').textContent = '正在读取会话状态…';
    dialog.showModal(); input.focus();
    const request = stateRequest = new AbortController();
    const timeout = setTimeout(() => request.abort(), 8000);
    try {
      const response = await fetch('/api/var-session', {cache:'no-store', signal:request.signal});
      if (!response.ok) throw new Error();
      const state = await response.json();
      if (request !== stateRequest || !dialog.open) return;
      if (!state.enabled) {message('当前服务不支持更新，请先升级监控服务。', true); return;}
      csrf = state.csrf_token; stateText(state);
    } catch {
      if (request === stateRequest && dialog.open) message('无法读取会话状态，请检查 SSH 隧道后重新打开。', true);
    } finally {clearTimeout(timeout); buttons();}
  };
  input.addEventListener('input', buttons);
  cancel.onclick = () => dialog.close();
  dialog.addEventListener('cancel', event => {if (sending) event.preventDefault();});
  dialog.addEventListener('close', () => {input.value = ''; csrf = ''; stateRequest?.abort(); stateRequest = null; message(''); buttons();});
  $('var-session-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (sending || !csrf || !input.value.trim()) return;
    let token = input.value.trim();
    input.value = ''; sending = true; buttons();
    save.textContent = '正在验证…'; message('正在验证新会话，原会话保持可用。');
    const request = new AbortController(), timeout = setTimeout(() => request.abort(), 25000);
    try {
      const pending = fetch('/api/var-session', {method:'POST',cache:'no-store',
        headers:{'Content-Type':'application/json','X-Session-Token':csrf},
        body:JSON.stringify({token}),signal:request.signal});
      token = '';
      const response = await pending, result = await response.json();
      if (!response.ok) {message(result.error || '更新失败，请重新打开后重试。', true); return;}
      stateText(result.session);
      message('已验证并保存。策略将自动读取新会话，等待下一份鉴权报价；无需重启。');
      $('refresh').click();
    } catch {
      message('提交结果尚未确认，不会自动重发。请重新打开查看会话状态，再决定是否重试。', true);
    } finally {token = ''; clearTimeout(timeout); sending = false; save.textContent = '验证并保存'; buttons();}
  });
})();
