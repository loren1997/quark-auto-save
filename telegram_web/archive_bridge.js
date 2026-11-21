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
    state.botJobs = new Map();
  }
  const botJobs = state.botJobs ||= new Map();
  for (const [payload, job] of botJobs) {
    if (job.created < Date.now() - 15 * 60 * 1000) botJobs.delete(payload);
  }
  const searches = state.searches;
  for (const [name, cached] of searches) {
    // Unfinished pages must survive daily/weekly runs; the cache still holds at most 20 queries.
    if (cached.used < Date.now() - 14 * 24 * 60 * 60 * 1000 || cached.done) searches.delete(name);
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
  let botPeer, activeJob, detailRequest;
  let botChecked = false;
  const bot = {requests: 0, links: 0, pending: 0, failed: 0, channel_posts: 0, buttons: 0, channel_archived: 0,
    details_read: 0, details_failed: 0, button_count: 0};
  const isOscarChannel = entity => entity.input._ === 'inputPeerChannel' && (
    [entity.username, ...(entity.usernames || [])].some(name => name?.toLowerCase() === 'oscar_4kmovies')
    || /奥斯卡.*(?:4k|蓝光).*(?:影视|磁力)/i.test(String(entity.title || '').normalize('NFKC')));
  function assertAccount() {
    if (String(window.rootScope?.myId) !== account) throw {type: 'ACCOUNT_CHANGED'};
  }
  async function invoke(method, params) {
    assertAccount();
    const archiveRead = ['messages.getDialogs', 'messages.getPinnedDialogs', 'messages.searchGlobal'].includes(method)
      && params.folder_id === 1;
    // Only a validated archived resource button can authorize this one bot's private history/start.
    const botRead = activeJob && (method === 'contacts.resolveUsername' && params.username === 'Oscar4KBot'
      || botPeer && params.peer?._ === 'inputPeerUser' && params.peer.user_id === botPeer.user_id
        && params.peer.access_hash === botPeer.access_hash && (method === 'messages.getHistory'
          || method === 'messages.startBot' && params.start_param === activeJob.payload
            && params.bot?._ === 'inputUser' && params.bot.user_id === botPeer.user_id
            && params.bot.access_hash === botPeer.access_hash));
    const channelRead = archive?.complete
      && [...archive.peers.values()].some(entity => isOscarChannel(entity)
        && (method === 'messages.search' && params.peer?._ === 'inputPeerChannel'
          && params.peer.channel_id === entity.input.channel_id && params.peer.access_hash === entity.input.access_hash
          || method === 'channels.getMessages' && detailRequest?.peerKey === `c:${entity.input.channel_id}`
            && params.channel?._ === 'inputChannel' && params.channel.channel_id === entity.input.channel_id
            && params.channel.access_hash === entity.input.access_hash && Array.isArray(params.id)
            && params.id.length > 0 && params.id.length <= 20
            && params.id.every(id => id._ === 'inputMessageID' && detailRequest.ids.has(id.id))));
    if (!archiveRead && !botRead && !channelRead) throw {type: 'BRIDGE_UNAVAILABLE'};
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
        title: chat.title, username: chat.username,
        usernames: (chat.usernames || []).filter(item => item.pFlags?.active).map(item => item.username),
        publicMessages: true, readable});
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
  const buttons = message => (message.reply_markup?.rows || []).flatMap(row => row.buttons || []);
  function shares(message) {
    const text = String(message.message || '');
    const links = [text,
      ...(message.entities || []).filter(e => e._ === 'messageEntityTextUrl').map(e => e.url || ''),
      ...buttons(message).filter(b => b._ === 'keyboardButtonUrl').map(b => b.url || ''),
      ...buttons(message).filter(b => b._ === 'keyboardButtonCopy').map(b => b.copy_text || '')].join('\n');
    const ids = new Set([...links.matchAll(/https?:\/\/pan\.quark\.cn\/s\/([A-Za-z0-9]{6,32})(?![A-Za-z0-9])/g)].map(m => m[1]));
    return [...links.matchAll(/https?:\/\/pan\.quark\.cn\/s\/[A-Za-z0-9]{6,32}(?![A-Za-z0-9])(?:[?#][^\s<>"'，。；、）】]*)?/g)].map(match => {
      const parsed = new URL(match[0]);
      parsed.protocol = 'https:';
      if (!parsed.searchParams.has('pwd') && ids.size === 1) {
        const passcode = /(?:提取码|访问码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])/i.exec(text);
        if (passcode) parsed.searchParams.set('pwd', passcode[1]);
      }
      return parsed.toString();
    });
  }
  function addShares(message, post, entity, priority = false) {
    const text = String(post.message || '');
    for (const url of shares(message)) {
      if (search.urls.has(url)) continue;
      search.urls.add(url);
      const title = text.split('\n').map(s => s.trim()).find(s => s && !/^https?:\/\//.test(s)) || entity.title || '';
      const candidate = {title: title.slice(0,240), url, content: text.slice(0,1600), source_type: 'telegram_web'};
      if (entity.publicMessages && /^[a-zA-Z][a-zA-Z0-9_]{4,31}$/.test(entity.username || '') && Number.isInteger(post.id) && post.id > 0) {
        candidate.source_url = `https://t.me/${entity.username}/${post.id}`;
      }
      const item = {peerKey: key(post.peer_id), candidate};
      if (priority) { search.pending.unshift(item); bot.links++; }
      else search.pending.push(item);
    }
  }
  function resourcePayload(button) {
    const label = String(button.text || '').normalize('NFKC').replace(/[\s\u200b-\u200f\ufeff]/g, '');
    if (button._ !== 'keyboardButtonUrl' || !/(获取资源|獲取資源)/.test(label)) return;
    try {
      const url = new URL(button.url);
      if (url.username || url.password || url.port || url.hash) return;
      const tg = url.protocol === 'tg:' && url.hostname === 'resolve' && !url.pathname
        && url.searchParams.get('domain')?.toLowerCase() === 'oscar4kbot';
      const web = url.protocol === 'https:' && url.hostname === 't.me' && url.pathname.toLowerCase() === '/oscar4kbot';
      if (!tg && !web) return;
      const keys = [...url.searchParams.keys()];
      if (new Set(keys).size !== keys.length || keys.some(k => k !== 'start' && !(tg && k === 'domain'))) return;
      const payload = url.searchParams.get('start');
      if (/^get[0-9]{1,20}$/.test(payload || '')) return payload;
    } catch (_) { /* An unsupported button is not an executable action. */ }
  }
  async function resolveResourceButton() {
    if (botChecked || !search.botPending.length || until - Date.now() < 1000) return;
    botChecked = true;
    const item = search.botPending[0], entity = archive.peers.get(item.peerKey);
    if (!entity) { search.botPending.shift(); return; }
    let job = botJobs.get(item.payload);
    if (!job) {
      while (botJobs.size >= 100) botJobs.delete(botJobs.keys().next().value);
      job = {payload: item.payload, created: Date.now(), polls: 0};
      botJobs.set(item.payload, job);
    }
    activeJob = job;
    try {
      if (!job.replies && !job.failed) {
        const resolved = await invoke('contacts.resolveUsername', {username: 'Oscar4KBot'});
        const user = (resolved.users || []).find(u => u._ === 'user' && u.id === resolved.peer?.user_id);
        if (!user?.pFlags?.bot || user.pFlags.deleted || user.pFlags.restricted || !user.access_hash
          || user.username?.toLowerCase() !== 'oscar4kbot') throw {type: 'BOT_UNAVAILABLE'};
        botPeer = {_: 'inputPeerUser', user_id: user.id, access_hash: user.access_hash};
        const history = min => invoke('messages.getHistory', {peer: botPeer, offset_id: 0, offset_date: 0,
          add_offset: 0, limit: min === null ? 1 : 10, max_id: 0, min_id: min || 0, hash: '0'});
        if (!job.started) {
          const before = await history(null);
          if (!Array.isArray(before.messages)) throw {type: 'BOT_UNAVAILABLE'};
          job.baseline = Math.max(0, ...before.messages.map(m => Number(m.id) || 0));
          if (until - Date.now() < 500) throw {type: 'BUDGET_EXHAUSTED'};
          const random = crypto.getRandomValues(new Uint32Array(2));
          job.randomId = (BigInt.asIntN(64, (BigInt(random[0]) << 32n) | BigInt(random[1])) || 1n).toString();
          job.started = true; // An ambiguous timeout must not send another /start on the next batch.
          bot.requests++;
          const started = await invoke('messages.startBot', {bot: {_: 'inputUser', user_id: user.id,
            access_hash: user.access_hash}, peer: botPeer, random_id: job.randomId, start_param: job.payload});
          job.requestId = (started.updates || []).map(u => u.message).find(m => m?.pFlags?.out
            && key(m.peer_id) === `u:${user.id}` && m.message === `/start ${job.payload}`)?.id;
        }
        const end = Math.min(until - 300, Date.now() + 6000);
        const normalize = value => String(value).normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
        do {
          const response = await history(job.baseline);
          if (!Array.isArray(response.messages)) throw {type: 'BOT_UNAVAILABLE'};
          const replies = response.messages.filter(m => m._ === 'message' && !m.pFlags?.out
            && m.id > job.baseline && key(m.peer_id) === `u:${user.id}` && (!m.from_id || key(m.from_id) === `u:${user.id}`)
            && (job.requestId && m.reply_to?.reply_to_msg_id === job.requestId
              || normalize(m.message || '').includes(normalize(query))) && shares(m).length);
          if (replies.length) { job.replies = replies; break; }
          if (Date.now() + 600 >= end) break;
          await new Promise(resolve => setTimeout(resolve, 500));
        } while (Date.now() < end);
        if (!job.replies && ++job.polls >= 3) job.failed = true;
      }
      if (job.replies) {
        for (const reply of job.replies) addShares(reply, item.post, entity, true);
        search.botPending.shift();
      } else if (job.failed) { bot.failed++; search.botPending.shift(); }
    } catch (error) {
      const type = String(error?.type || '');
      if (/FLOOD|ACCOUNT_CHANGED|AUTH_KEY|SESSION_|BUDGET_EXHAUSTED/.test(type)) throw error;
      job.failed = true; bot.failed++; search.botPending.shift();
    } finally {
      activeJob = null; botPeer = null;
    }
  }
  async function messageDetails(messages, channel) {
    const peerKey = `c:${channel.input.channel_id}`;
    const ids = new Set(messages.filter(message => key(message.peer_id) === peerKey
      && message._ === 'message' && Number.isInteger(message.id) && message.id > 0 && !buttons(message).length)
      .slice(0, 20).map(message => message.id));
    if (!ids.size) return messages;
    detailRequest = {peerKey, ids};
    try {
      // Search previews can omit inline keyboards; read these exact archived posts once in a batch.
      const response = await invoke('channels.getMessages', {
        channel: {_: 'inputChannel', channel_id: channel.input.channel_id, access_hash: channel.input.access_hash},
        id: [...ids].map(id => ({_: 'inputMessageID', id}))});
      if (!Array.isArray(response.messages)) throw {type: 'BRIDGE_UNAVAILABLE'};
      const full = new Map(response.messages.filter(message => key(message.peer_id) === peerKey
        && message._ === 'message' && ids.has(message.id)).map(message => [message.id, message]));
      bot.details_read += full.size;
      bot.details_failed += ids.size - full.size;
      return messages.map(message => full.get(message.id) || message);
    } catch (error) {
      if (/FLOOD|ACCOUNT_CHANGED|AUTH_KEY|SESSION_|BUDGET_EXHAUSTED/.test(String(error?.type || ''))) throw error;
      bot.details_failed += ids.size;
      return messages;
    } finally { detailRequest = null; }
  }
  function collectMessages(messages) {
    for (const message of messages) {
      search.seen++;
      const entity = archive.peers.get(key(message.peer_id));
      if (!entity || message._ !== 'message') continue;
      addShares(message, message, entity);
      bot.button_count += buttons(message).length;
      for (const button of buttons(message)) {
        const payload = resourcePayload(button);
        if (payload && !search.botSeen.has(payload)) {
          search.botSeen.add(payload); bot.buttons++;
          search.botPending.push({payload, post: {id: message.id, peer_id: message.peer_id,
            message: message.message}, peerKey: key(message.peer_id)});
        }
      }
    }
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
        done: false, exhausted: false, pending: [], urls: new Set(), botPending: [], botSeen: new Set(), used: Date.now()};
      searches.set(query, search);
    }
    const drain = () => {
      while (search.pending.length && results.length < limit) {
        const item = search.pending.shift();
        if (archive.peers.has(item.peerKey)) results.push(item.candidate);
      }
    };
    const channel = [...archive.peers.values()].find(isOscarChannel);
    bot.channel_archived = channel ? 1 : 0;
    const channelKey = channel ? `c:${channel.input.channel_id}` : '';
    if (channel && search.channelChecked !== channelKey) {
      if (channel) {
        try {
          const response = await invoke('messages.search', {peer: channel.input, q: query,
            filter: {_: 'inputMessagesFilterEmpty'}, min_date: 0, max_date: 0, offset_id: 0,
            add_offset: 0, limit: 20, max_id: 0, min_id: 0, hash: '0'});
          if (!Array.isArray(response.messages)) throw {type: 'BRIDGE_UNAVAILABLE'};
          bot.channel_posts = response.messages.length;
          const posts = response.messages.filter(message => key(message.peer_id) === `c:${channel.input.channel_id}`);
          collectMessages(await messageDetails(posts, channel));
        } catch (error) {
          if (/FLOOD|ACCOUNT_CHANGED|AUTH_KEY|SESSION_|BUDGET_EXHAUSTED/.test(String(error?.type || ''))) throw error;
          bot.failed++;
        }
      }
      search.channelChecked = channelKey;
    }
    await resolveResourceButton();
    drain();
    while (!search.exhausted && results.length < limit && archive.peers.size) {
      const response = await invoke('messages.searchGlobal', {folder_id: 1, q: query,
        filter: {_: 'inputMessagesFilterEmpty'}, min_date: 0, max_date: 0,
        offset_rate: search.rate, offset_peer: search.peer, offset_id: search.id, limit: 100});
      if (!Array.isArray(response.messages)) throw {type: 'BRIDGE_UNAVAILABLE'};
      const entities = peers(response);
      search.total = Math.max(search.total, Number(response.count) || response.messages.length);
      collectMessages(response.messages);
      await resolveResourceButton();
      drain();
      if (response._ === 'messages.messages' || !response.messages.length) { search.exhausted = true; break; }
      const last = response.messages[response.messages.length - 1];
      const id = key(last.peer_id), entity = entities.get(id) || archive.peers.get(id);
      const nextRate = response.next_rate ?? last.date ?? 0;
      const cursor = `${id}/${last.id}/${nextRate}`;
      if (!entity || !last.id || cursor === search.cursor) throw {type: 'PAGINATION_FAILED'};
      search.cursor = cursor; search.id = last.id; search.peer = entity.input; search.rate = nextRate;
    }
    if (!archive.peers.size) search.exhausted = true;
    bot.pending = search.botPending.length;
    search.done = search.exhausted && !search.pending.length && !bot.pending;
    if (search.done) phase = 'complete';
    return {success: true, results, archived_count: archive.peers.size,
      partial: !search.done, progress: progress(), bot};
  } catch (error) {
    const type = String(error?.type || error?.message || '');
    const flood = /FLOOD(?:_PREMIUM)?_WAIT_(\d+)/.exec(type);
    let code = flood ? 'FLOOD_WAIT' : ['ACCOUNT_CHANGED','AUTH_KEY_UNREGISTERED','SESSION_REVOKED','SESSION_EXPIRED'].includes(type)
      ? 'AUTH_REQUIRED' : ['BUDGET_EXHAUSTED','PAGINATION_FAILED','BRIDGE_UNAVAILABLE'].includes(type) ? type : 'SEARCH_FAILED';
    return {success: code === 'BUDGET_EXHAUSTED', code, results,
      partial: true, archived_count: archive?.peers.size || 0, progress: progress(),
      bot: {...bot, pending: search?.botPending.length || 0},
      retry_after: flood ? Math.min(86400, Number(flood[1])) : 0};
  }
}
