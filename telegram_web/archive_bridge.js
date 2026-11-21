// Executed only in the already authenticated official WebK page. No credentials leave it.
async ({query, limit, timeout}) => {
  if (location.origin !== 'https://web.telegram.org' || location.pathname !== '/k/') {
    return {success: false, code: 'BRIDGE_UNAVAILABLE'};
  }
  const root = window.rootScope;
  const api = root?.managers?.apiManager;
  if (!root?.myId || !api?.invokeApi) return {success: false, code: 'AUTH_REQUIRED'};
  const account = String(root.myId);
  const until = Date.now() + Math.max(1000, Math.min(45000, timeout * 1000));
  const key = peer => peer?._ === 'peerUser' ? `u:${peer.user_id}`
    : peer?._ === 'peerChat' ? `g:${peer.chat_id}`
      : peer?._ === 'peerChannel' ? `c:${peer.channel_id}` : '';
  const state = window.__qasArchiveReadState ||= {account, archives: null, searches: new Map()};
  if (state.account !== account) {
    state.account = account;
    state.archives = null;
    state.searches = new Map();
  }
  const searches = state.searches;
  for (const [name, cached] of searches) {
    if (cached.used < Date.now() - 30 * 60 * 1000 || cached.done) searches.delete(name);
  }
  let search = searches.get(query);
  if (search) {
    searches.delete(query);
    searches.set(query, search);
    search.used = Date.now();
  }
  const empty = () => ({_: 'inputPeerEmpty'});
  const results = [];
  let phase = 'archives';
  let archive = state.archives;
  function assertAccount() {
    if (String(window.rootScope?.myId) !== account) throw {type: 'ACCOUNT_CHANGED'};
  }
  async function invoke(method, params) {
    assertAccount();
    // The only permitted history query is server-side archive scoped, never a fallback.
    if (!['messages.getDialogs', 'messages.getPinnedDialogs', 'messages.searchGlobal'].includes(method)
      || params.folder_id !== 1) throw {type: 'BRIDGE_UNAVAILABLE'};
    const remaining = until - Date.now();
    if (remaining < 250) throw {type: 'BUDGET_EXHAUSTED'};
    let timer;
    try {
      const value = await Promise.race([
        api.invokeApi(method, params, {
          noErrorBox: true, ignoreErrors: true, noRetry: true, rawError: true,
          floodMaxTimeout: 0, stopTime: until
        }),
        new Promise((_, reject) => { timer = setTimeout(() => reject({type: 'BUDGET_EXHAUSTED'}), remaining); })
      ]);
      assertAccount();
      return value;
    } finally { clearTimeout(timer); }
  }
  function peers(response) {
    const entities = new Map();
    for (const user of response.users || []) {
      if (user._ !== 'user') continue;
      entities.set(`u:${user.id}`, {input: user.pFlags?.self ? {_: 'inputPeerSelf'}
        : {_: 'inputPeerUser', user_id: user.id, access_hash: user.access_hash},
      readable: !user.pFlags?.deleted,
      title: [user.first_name, user.last_name].filter(Boolean).join(' '), username: user.username});
    }
    for (const chat of response.chats || []) {
      const readable = !chat.pFlags?.restricted && !chat.pFlags?.kicked && !chat._.endsWith('Forbidden');
      // Unavailable peers still supply the cursor needed to reach later dialogs.
      if (chat._ === 'chat' || chat._ === 'chatForbidden') entities.set(`g:${chat.id}`, {
        input: {_: 'inputPeerChat', chat_id: chat.id}, title: chat.title, readable});
      if (chat._ === 'channel' || chat._ === 'channelForbidden') entities.set(`c:${chat.id}`, {
        input: {_: 'inputPeerChannel', channel_id: chat.id, access_hash: chat.access_hash},
        title: chat.title, username: chat.username, publicMessages: true, readable});
    }
    return entities;
  }
  function saveDialogs(response, entities) {
    for (const dialog of response.dialogs || []) {
      // WebK documents that pinned main-folder entries can leak into folder responses.
      if (dialog._ !== 'dialog' || dialog.folder_id !== 1) continue;
      const id = key(dialog.peer), entity = entities.get(id);
      if (id && entity?.readable) archive.peers.set(id, entity);
    }
  }
  function progress() {
    return {phase, current: phase === 'archives' ? archive?.peers.size || 0 : search?.seen || 0,
      total: phase === 'archives' ? archive?.total || 0 : search?.total || 0};
  }
  try {
    // Refresh the complete archive membership for every request. A timed-out
    // archive listing resumes from its cursor; it is never treated as the whole folder.
    if (!archive || archive.complete) {
      archive = state.archives = {peers: new Map(), pinned: false, complete: false,
        date: 0, id: 0, peer: empty(), total: 0, cursor: ''};
    }
    if (!archive.pinned) {
      const response = await invoke('messages.getPinnedDialogs', {folder_id: 1});
      if (!Array.isArray(response.dialogs)) throw {type: 'BRIDGE_UNAVAILABLE'};
      saveDialogs(response, peers(response));
      archive.pinned = true;
    }
    while (!archive.complete) {
      const response = await invoke('messages.getDialogs', {folder_id: 1, exclude_pinned: true,
        offset_date: archive.date, offset_id: archive.id, offset_peer: archive.peer, limit: 100, hash: '0'});
      if (!Array.isArray(response.dialogs) || !Array.isArray(response.messages)) throw {type: 'BRIDGE_UNAVAILABLE'};
      const entities = peers(response);
      saveDialogs(response, entities);
      archive.total = Math.max(archive.total, Number(response.count) || archive.peers.size);
      if (response._ === 'messages.dialogs' || !response.dialogs.length) { archive.complete = true; break; }
      const last = response.dialogs[response.dialogs.length - 1];
      const id = key(last.peer), entity = entities.get(id);
      const message = response.messages.find(m => key(m.peer_id) === id && m.id === last.top_message);
      const cursor = `${id}/${last.top_message}/${message?.date}`;
      if (!entity || !message?.date || cursor === archive.cursor) throw {type: 'PAGINATION_FAILED'};
      archive.cursor = cursor; archive.date = message.date; archive.id = last.top_message; archive.peer = entity.input;
    }
    phase = 'search';
    if (!search) {
      while (searches.size >= 20) searches.delete(searches.keys().next().value);
      search = {query, id: 0, peer: empty(), rate: 0, cursor: '', seen: 0, total: 0,
        done: false, exhausted: false, pending: [], urls: new Set(), used: Date.now()};
      searches.set(query, search);
    }
    const drain = () => {
      while (search.pending.length && results.length < limit) {
        const item = search.pending.shift();
        if (archive.peers.has(item.peerKey)) results.push(item.candidate);
      }
    };
    drain();
    while (!search.exhausted && results.length < limit && archive.peers.size) {
      const response = await invoke('messages.searchGlobal', {folder_id: 1, q: query,
        filter: {_: 'inputMessagesFilterEmpty'}, min_date: 0, max_date: 0,
        offset_rate: search.rate, offset_peer: search.peer, offset_id: search.id, limit: 100});
      if (!Array.isArray(response.messages)) throw {type: 'BRIDGE_UNAVAILABLE'};
      const entities = peers(response);
      search.total = Math.max(search.total, Number(response.count) || response.messages.length);
      for (const message of response.messages) {
        search.seen++;
        const entity = archive.peers.get(key(message.peer_id));
        if (!entity || message._ !== 'message') continue;
        // Only messages carrying real Quark shares cross the browser boundary.
        const text = String(message.message || '');
        const links = [text, ...(message.entities || []).filter(e => e._ === 'messageEntityTextUrl').map(e => e.url || '')].join('\n');
        const shareIds = new Set([...links.matchAll(/https?:\/\/pan\.quark\.cn\/s\/([A-Za-z0-9]{6,32})(?![A-Za-z0-9])/g)].map(m => m[1]));
        for (const match of links.matchAll(/https?:\/\/pan\.quark\.cn\/s\/[A-Za-z0-9]{6,32}(?![A-Za-z0-9])(?:[?#][^\s<>"'，。；、）】]*)?/g)) {
          const parsed = new URL(match[0]);
          parsed.protocol = 'https:';
          if (!parsed.searchParams.has('pwd') && shareIds.size === 1) {
            const passcode = /(?:提取码|访问码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])/i.exec(text);
            if (passcode) parsed.searchParams.set('pwd', passcode[1]);
          }
          const url = parsed.toString();
          if (search.urls.has(url)) continue;
          search.urls.add(url);
          const title = text.split('\n').map(s => s.trim()).find(s => s && !/^https?:\/\//.test(s)) || entity.title || '';
          const candidate = {title: title.slice(0,240), url, content: text.slice(0,1600), source_type: 'telegram_web'};
          if (entity.publicMessages && /^[a-zA-Z][a-zA-Z0-9_]{4,31}$/.test(entity.username || '') && Number.isInteger(message.id) && message.id > 0) {
            candidate.source_url = `https://t.me/${entity.username}/${message.id}`;
          }
          search.pending.push({peerKey: key(message.peer_id), candidate});
        }
      }
      drain();
      if (response._ === 'messages.messages' || !response.messages.length) { search.exhausted = true; break; }
      const last = response.messages[response.messages.length - 1];
      const id = key(last.peer_id), entity = entities.get(id) || archive.peers.get(id);
      const cursor = `${id}/${last.id}/${response.next_rate || 0}`;
      if (!entity || !last.id || cursor === search.cursor) throw {type: 'PAGINATION_FAILED'};
      search.cursor = cursor; search.id = last.id; search.peer = entity.input; search.rate = response.next_rate || 0;
    }
    if (!archive.peers.size) search.exhausted = true;
    search.done = search.exhausted && !search.pending.length;
    if (search.done) phase = 'complete';
    return {success: true, results, archived_count: archive.peers.size,
      partial: !search.done, progress: progress()};
  } catch (error) {
    const type = String(error?.type || error?.message || '');
    const flood = /FLOOD(?:_PREMIUM)?_WAIT_(\d+)/.exec(type);
    let code = flood ? 'FLOOD_WAIT' : ['ACCOUNT_CHANGED','AUTH_KEY_UNREGISTERED','SESSION_REVOKED','SESSION_EXPIRED'].includes(type)
      ? 'AUTH_REQUIRED' : ['BUDGET_EXHAUSTED','PAGINATION_FAILED','BRIDGE_UNAVAILABLE'].includes(type) ? type : 'SEARCH_FAILED';
    return {success: code === 'BUDGET_EXHAUSTED', code, results,
      partial: true, archived_count: archive?.peers.size || 0, progress: progress(),
      retry_after: flood ? Math.min(86400, Number(flood[1])) : 0};
  }
}
