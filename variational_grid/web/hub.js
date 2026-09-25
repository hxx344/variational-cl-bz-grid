(() => {
  'use strict';
  const embedded = window.parent !== window && /^p-[a-f0-9]{24}\.hub\.localhost$/.test(location.hostname);
  const origin = `${location.protocol}//hub.localhost${location.port ? ':' + location.port : ''}`;
  const listeners = new Set();
  let hostActive = !embedded, connected = false;
  const active = () => hostActive && !document.hidden && navigator.onLine !== false;
  let previous = active();
  const notify = () => {
    const next = active();
    if (next === previous) return;
    previous = next;
    for (const listener of listeners) listener(next);
  };
  const post = message => window.parent.postMessage({channel:'project-hub', version:1, ...message}, origin);
  window.GridHub = {active, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }};
  document.addEventListener('visibilitychange', notify);
  window.addEventListener('online', notify);
  window.addEventListener('offline', notify);
  if (!embedded) return;
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== origin) return;
    const message = event.data;
    if (!message || typeof message !== 'object' || Array.isArray(message) || message.channel !== 'project-hub' || message.version !== 1) return;
    if (message.type === 'ready' && message.role === 'host') {
      connected = true;
      post({type:'ready', role:'module', capabilities:['activity']});
    } else if (connected && message.type === 'activity' && typeof message.active === 'boolean') {
      hostActive = message.active;
      notify();
    }
  });
  post({type:'ready', role:'module'});
})();
