import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { z } from 'zod/v4';

const MAX_CONCURRENT_TOOL_CALLS = 3;

const ToggleAuxPaneResultSchema = z
  .object({
    isError: z.boolean().optional(),
  })
  .loose();

function stripUndefinedDeep(val) {
  if (Array.isArray(val)) {
    return val.map((item) => (item && typeof item === 'object' ? stripUndefinedDeep(item) : item));
  }
  if (!val || typeof val !== 'object') return val;
  const out = {};
  for (const [k, v] of Object.entries(val)) {
    if (v === undefined) continue;
    out[k] = v && typeof v === 'object' ? stripUndefinedDeep(v) : v;
  }
  return out;
}

function makeAbortError() {
  try {
    return new DOMException('Aborted', 'AbortError');
  } catch (_) {
    const err = new Error('Aborted');
    err.name = 'AbortError';
    return err;
  }
}

function extractTextFromContent(content) {
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim();
}

function unwrapToolResult(res) {
  if (!res || typeof res !== 'object') return res;
  if (res.isError) {
    const msg = extractTextFromContent(res.content) || 'MCP tool returned an error';
    const err = new Error(msg);
    err.isToolError = true;
    err.rawResult = res;
    throw err;
  }
  if (res.structuredContent !== undefined) {
    return res.structuredContent;
  }
  const txt = extractTextFromContent(res.content);
  if (txt) {
    try {
      return JSON.parse(txt);
    } catch (_) {
      return { text: txt };
    }
  }
  return res;
}

function applyHostContextToDocument(ctx) {
  if (!ctx || typeof ctx !== 'object') return;
  try {
    if (ctx.theme === 'light' || ctx.theme === 'dark') {
      applyDocumentTheme(ctx.theme);
    }
  } catch (_) {}
  try {
    if (ctx.styles && ctx.styles.variables && typeof ctx.styles.variables === 'object') {
      applyHostStyleVariables(ctx.styles.variables);
    }
  } catch (_) {}
  try {
    if (ctx.styles && ctx.styles.css && typeof ctx.styles.css.fonts === 'string' && ctx.styles.css.fonts) {
      applyHostFonts(ctx.styles.css.fonts);
    }
  } catch (_) {}
}

function initChengmenMcpBridge() {
  const app = new App({ name: 'Chengmen Paper Viewer', version: '0.3.0' }, {});

  let connected = false;
  let connectError = null;
  let lastToolInput = null;
  let lastToolResult = null;
  let lastHostContext = null;

  const toolInputListeners = new Set();
  const toolResultListeners = new Set();
  const toolCancelledListeners = new Set();
  const hostContextListeners = new Set();

  // Register ALL handlers BEFORE app.connect() so no initial notification is missed.
  app.ontoolinput = (params) => {
    lastToolInput = params || {};
    toolInputListeners.forEach((fn) => {
      try {
        fn(lastToolInput);
      } catch (err) {
        console.error('[chengmen-bridge] onToolInput listener error:', err);
      }
    });
  };

  app.ontoolresult = (result) => {
    lastToolResult = result || {};
    toolResultListeners.forEach((fn) => {
      try {
        fn(lastToolResult);
      } catch (err) {
        console.error('[chengmen-bridge] onToolResult listener error:', err);
      }
    });
  };

  app.ontoolcancelled = (params) => {
    toolCancelledListeners.forEach((fn) => {
      try {
        fn(params || {});
      } catch (err) {
        console.error('[chengmen-bridge] onToolCancelled listener error:', err);
      }
    });
  };

  app.onhostcontextchanged = (ctx) => {
    if (ctx && typeof ctx === 'object') {
      lastHostContext = { ...(lastHostContext || {}), ...ctx };
      applyHostContextToDocument(ctx);
      hostContextListeners.forEach((fn) => {
        try {
          fn(lastHostContext);
        } catch (err) {
          console.error('[chengmen-bridge] onHostContextChanged listener error:', err);
        }
      });
    }
  };

  app.onteardown = async () => ({});

  // Priority-aware tool call scheduler enforcing <= 3 concurrent calls (host cap is 4).
  let activeCalls = 0;
  let nextSeq = 0;
  const pendingQueue = [];

  function pumpQueue() {
    while (activeCalls < MAX_CONCURRENT_TOOL_CALLS && pendingQueue.length > 0) {
      pendingQueue.sort((a, b) => (b.priority - a.priority) || (a.seq - b.seq));
      const job = pendingQueue.shift();
      if (!job) break;
      if (job.signal && job.signal.aborted) {
        job.reject(makeAbortError());
        continue;
      }
      activeCalls += 1;
      const reqOpts = job.signal ? { signal: job.signal } : undefined;
      Promise.resolve()
        .then(() => app.callServerTool({ name: job.name, arguments: job.args }, reqOpts))
        .then((res) => {
          job.resolve(unwrapToolResult(res));
        })
        .catch((err) => {
          job.reject(err);
        })
        .finally(() => {
          activeCalls = Math.max(0, activeCalls - 1);
          pumpQueue();
        });
    }
  }

  const ready = (async () => {
    try {
      await app.connect();
      connected = true;
      const ctx = app.getHostContext();
      if (ctx) {
        lastHostContext = { ...(lastHostContext || {}), ...ctx };
        applyHostContextToDocument(lastHostContext);
      }
      return bridge;
    } catch (err) {
      connectError = err;
      throw err;
    }
  })();

  function getCapabilities() {
    const raw = connected ? (app.getHostCapabilities() || {}) : {};
    return {
      serverTools: Boolean(raw.serverTools),
      message: Boolean(raw.message),
      updateModelContext: Boolean(raw.updateModelContext),
      openLinks: Boolean(raw.openLinks),
      raw,
    };
  }

  function callTool(name, args = {}, priorityOrOpts = 0) {
    let priority = 0;
    let signal = null;
    if (typeof priorityOrOpts === 'number') {
      priority = priorityOrOpts;
    } else if (priorityOrOpts && typeof priorityOrOpts === 'object') {
      priority = typeof priorityOrOpts.priority === 'number' ? priorityOrOpts.priority : 0;
      signal = priorityOrOpts.signal || null;
    }

    if (signal && signal.aborted) {
      return Promise.reject(makeAbortError());
    }

    return ready.then(
      () =>
        new Promise((resolve, reject) => {
          if (signal && signal.aborted) {
            reject(makeAbortError());
            return;
          }
          const job = {
            name: String(name),
            args: args && typeof args === 'object' ? stripUndefinedDeep(args) : {},
            priority,
            signal,
            seq: nextSeq++,
            resolve,
            reject,
          };
          if (signal) {
            const onAbort = () => {
              const idx = pendingQueue.indexOf(job);
              if (idx !== -1) {
                pendingQueue.splice(idx, 1);
                reject(makeAbortError());
              }
            };
            signal.addEventListener('abort', onAbort, { once: true });
          }
          pendingQueue.push(job);
          pumpQueue();
        }),
    );
  }

  async function sendMessage(text, opts = {}) {
    await ready;
    if (!getCapabilities().message) {
      return { ok: false, isError: true, unsupported: true };
    }
    const params = {
      role: 'user',
      content: [{ type: 'text', text: String(text) }],
    };
    if (opts && opts._meta && typeof opts._meta === 'object') {
      params._meta = opts._meta;
    }
    const res = await app.sendMessage(params);
    return { ok: !res?.isError, ...(res || {}) };
  }

  async function updateModelContext(input, structuredContent) {
    await ready;
    if (!getCapabilities().updateModelContext) {
      return { ok: false, isError: true, unsupported: true };
    }
    let params;
    if (typeof input === 'string') {
      params = { content: [{ type: 'text', text: input }] };
    } else if (Array.isArray(input)) {
      params = { content: input };
    } else if (input && typeof input === 'object') {
      params = { ...input };
    } else {
      params = { content: [] };
    }
    if (structuredContent && typeof structuredContent === 'object') {
      params.structuredContent = structuredContent;
    }
    const res = await app.updateModelContext(params);
    return { ok: true, ...(res || {}) };
  }

  async function openLink(urlOrParams) {
    await ready;
    const url = typeof urlOrParams === 'string' ? urlOrParams : urlOrParams?.url;
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url.trim())) {
      return { ok: false, isError: true, error: 'Only http/https links are supported' };
    }
    if (!getCapabilities().openLinks) {
      return { ok: false, isError: true, unsupported: true };
    }
    const res = await app.openLink({ url: url.trim() });
    return { ok: !res?.isError, ...(res || {}) };
  }

  async function openFileInAuxPane(fileUri) {
    await ready;
    if (typeof fileUri !== 'string' || !fileUri.startsWith('file://')) {
      return { ok: false, isError: true, error: 'Invalid file:// URI' };
    }
    try {
      const res = await app.request(
        {
          method: 'com.google.antigravity/toggle-aux-pane',
          params: { tabType: 'fileView', uri: fileUri },
        },
        ToggleAuxPaneResultSchema,
      );
      if (res && res.isError) {
        return { ok: false, isError: true };
      }
      return { ok: true, ...(res || {}) };
    } catch (err) {
      const code = err?.code;
      const msg = String(err?.message || err || '');
      const unsupported = code === -32601 || msg.includes('-32601') || msg.toLowerCase().includes('method not found');
      return { ok: false, isError: true, unsupported, error: msg };
    }
  }

  function onToolInput(fn) {
    if (typeof fn !== 'function') return () => {};
    toolInputListeners.add(fn);
    if (lastToolInput !== null) {
      try {
        fn(lastToolInput);
      } catch (err) {
        console.error('[chengmen-bridge] replay onToolInput error:', err);
      }
    }
    return () => toolInputListeners.delete(fn);
  }

  function onToolResult(fn) {
    if (typeof fn !== 'function') return () => {};
    toolResultListeners.add(fn);
    if (lastToolResult !== null) {
      try {
        fn(lastToolResult);
      } catch (err) {
        console.error('[chengmen-bridge] replay onToolResult error:', err);
      }
    }
    return () => toolResultListeners.delete(fn);
  }

  function onToolCancelled(fn) {
    if (typeof fn !== 'function') return () => {};
    toolCancelledListeners.add(fn);
    return () => toolCancelledListeners.delete(fn);
  }

  function onHostContextChanged(fn) {
    if (typeof fn !== 'function') return () => {};
    hostContextListeners.add(fn);
    if (lastHostContext !== null) {
      try {
        fn(lastHostContext);
      } catch (err) {
        console.error('[chengmen-bridge] replay onHostContextChanged error:', err);
      }
    }
    return () => hostContextListeners.delete(fn);
  }

  const bridge = {
    ready,
    isConnected: () => connected,
    getConnectError: () => connectError,
    callTool,
    getCapabilities,
    getHostContext: () => lastHostContext || (connected ? app.getHostContext() : null),
    sendMessage,
    updateModelContext,
    openLink,
    openFileInAuxPane,
    onToolInput,
    onToolResult,
    onToolCancelled,
    onHostContextChanged,
    maxConcurrentCalls: MAX_CONCURRENT_TOOL_CALLS,
  };

  return bridge;
}

if (typeof window !== 'undefined') {
  window.__CHENGMEN_MCP_BRIDGE__ = initChengmenMcpBridge();
}
