/* Recent navigation data lives only in this tab, never in browser storage. */
globalThis.MoyaiNavigationCache = {
  pathsFor({view, runId, role, start, end}) {
    if (/^[a-f0-9]{32}$/.test(runId || '')) return [`/api/runs/${runId}?activity=summary`];
    const paths = {
      tasks: ['/api/config', '/api/connections', '/api/environments'],
      settings: ['/api/session', '/api/settings/session-titles'],
      users: role === 'admin' ? ['/api/session', '/api/admin/users'] : ['/api/session'],
      connections: ['/api/connections', '/api/organization'],
      skills: ['/api/skills?archived=true'], memory: ['/api/memory'],
      automations: ['/api/automations'], runtime: ['/api/config'],
      environments: role === 'admin' ? ['/api/admin/environments'] : [],
    };
    if (view === 'spend' || view === 'adoption') {
      const query = new URLSearchParams();
      if (start) query.set('start', start);
      if (end) query.set('end', end);
      return ['/api/spend?' + query];
    }
    return paths[view] || [];
  },
  create({maxEntries = 40, maxBytes = 8 * 1024 * 1024, maxAge = 15000, now = Date.now} = {}) {
    const entries = new Map(), pending = new Map();
    let bytes = 0, generation = 0;
    const allowed = path => /^\/api\/runs\/[a-f0-9]{32}\?activity=summary$/.test(path) ||
      /^\/api\/runs\/[a-f0-9]{32}\/pull-request\?url=/.test(path) ||
      ['/api/session', '/api/config', '/api/organization', '/api/connections',
        '/api/environments', '/api/admin/environments', '/api/admin/users',
        '/api/admin/identities/status', '/api/settings/session-titles',
        '/api/skills?archived=true', '/api/memory', '/api/automations'].includes(path) ||
      /^\/api\/(spend|admin\/(adoption|pull-requests))\?/.test(path);
    function forget(path) {
      const entry = entries.get(path);
      if (entry) bytes -= entry.bytes;
      entries.delete(path);
      pending.delete(path);
    }
    function clear() { generation++; entries.clear(); pending.clear(); bytes = 0; }
    function put(path, value) {
      if (!allowed(path)) return;
      const text = JSON.stringify(value), size = text.length * 2;
      const old = entries.get(path);
      if (old) { bytes -= old.bytes; entries.delete(path); }
      if (size > maxBytes / 2) return;
      entries.set(path, {text, bytes: size, time: now()}); bytes += size;
      while (entries.size > maxEntries || bytes > maxBytes) {
        const key = entries.keys().next().value, entry = entries.get(key);
        bytes -= entry.bytes; entries.delete(key);
      }
    }
    function get(path, age = maxAge) {
      const entry = entries.get(path);
      if (!entry || now() - entry.time >= age) return undefined;
      entries.delete(path); entries.set(path, entry);
      // Controllers merge live events into their own copy of the response.
      return JSON.parse(entry.text);
    }
    function read(path, load, {recent = false} = {}) {
      if (!allowed(path)) return load();
      if (recent) {
        const value = get(path);
        if (value !== undefined) return Promise.resolve(value);
        if (pending.has(path)) return pending.get(path).then(value => structuredClone(value));
      }
      const epoch = generation;
      const request = load().then(value => {
        if (epoch === generation && pending.get(path) === request) put(path, value);
        return value;
      }).catch(error => {
        if (epoch === generation && pending.get(path) === request) {
          if (error.status === 401 || error.status === 403) clear();
          else if (error.status === 404 || error.status === 409) forget(path);
        }
        throw error;
      }).finally(() => { if (pending.get(path) === request) pending.delete(path); });
      pending.set(path, request);
      return request;
    }
    return {get, put, read, clear, forget, get pendingCount() { return pending.size; }};
  },
};
