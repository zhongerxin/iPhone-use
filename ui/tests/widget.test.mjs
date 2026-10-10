import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const source = await readFile(new URL('../src/app.ts', import.meta.url), 'utf8');
const codes = {};
for (const language of ['default', 'ja']) {
  const { outputFiles } = await build({
    stdin: { contents: source.replace("import { App } from '@modelcontextprotocol/ext-apps';", 'const App = globalThis.MockApp;'),
      loader: 'ts', resolveDir: fileURLToPath(new URL('../src/', import.meta.url)) },
    bundle: true, format: 'esm', target: 'es2022', write: false,
    define: { __IPHONE_USE_LANGUAGE__: JSON.stringify(language) },
  });
  codes[language] = `(async () => {\n${outputFiles[0].text}\n})()`;
}

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const preview = (fields = {}) => ({ server_time: 1000, frame: null, viewport: null, busy: false, paused: false, events: [], ...fields });
// Fixtures exist only in this isolated DOM test, never in the shipped widget.
const frame = (seq = 1) => ({ seq, data: 'test-image-only', mimeType: 'image/png', width: 900, height: 1800 });

async function harness({ language = 'default', reducedMotion = false, context = { displayMode: 'inline', availableDisplayModes: ['fullscreen'] }, reply, connectReply } = {}) {
  let now = 0;
  let timerId = 0;
  let instance;
  let resize;
  const timers = new Map();
  const listeners = new Map();
  const calls = [];
  const lifecycle = [];
  const requestOptions = [];
  let layoutReads = 0;
  let imageWrites = 0;
  const ids = ['app', 'device', 'screen', 'image', 'empty-state', 'empty-state-text', 'cursor', 'stage', 'model', 'live-text', 'toast',
    'tool-refresh', 'tool-home', 'tool-screenshot'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    style: { setProperty(name, value) { this[name] = value; } }, dataset: {},
    hidden: ['device', 'screen', 'cursor', 'toast'].includes(id), clientWidth: 400, clientHeight: 800,
    disabled: false, textContent: '', handlers: new Map(), children: [],
    addEventListener(name, fn) { this.handlers.set(name, fn); },
    removeEventListener(name) { this.handlers.delete(name); },
    click() { this.handlers.get('click')?.(); },
    removeAttribute(name) { delete this[name]; },
    cloneNode() { return { ...this, style: { ...this.style }, dataset: { ...this.dataset }, children: [], animation: undefined }; },
    appendChild(node) { this.children.push(node); node.parent = this; return node; },
    remove() { const children = this.parent?.children; if (children?.includes(this)) children.splice(children.indexOf(this), 1); },
    animate(frames, options) {
      const animation = { frames, options, cancelled: false, cancel() { this.cancelled = true; } };
      this.animation = animation;
      return animation;
    },
  }]));
  // The stage is the area left between the status pill and the toolbar; reading it is a layout read.
  const stageSize = { width: 376, height: 776 };
  Object.defineProperty(elements.stage, 'clientWidth', { configurable: true, get: () => { layoutReads++; return stageSize.width; } });
  Object.defineProperty(elements.stage, 'clientHeight', { configurable: true, get: () => stageSize.height });
  let imageSource;
  Object.defineProperty(elements.image, 'src', {
    configurable: true, get: () => imageSource, set: value => { imageSource = value; imageWrites++; },
  });
  elements.image.removeAttribute = name => { if (name === 'src') imageSource = undefined; };
  class MockApp {
    constructor(info, capabilities, options) { Object.assign(this, { info, capabilities, options }); instance = this; }
    async connect() { lifecycle.push('connect'); if (connectReply) await connectReply(); }
    async close() { this.onclose?.(); }
    getHostContext() { return context; }
    async requestDisplayMode(params) { lifecycle.push(params.mode); return { mode: params.mode }; }
    callServerTool(params, options) {
      calls.push(params);
      requestOptions.push(options);
      return reply ? reply(params) : Promise.resolve({ structuredContent: preview() });
    }
  }
  const document = {
    hidden: false,
    documentElement: { dataset: {} },
    getElementById: id => elements[id],
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: name => listeners.delete(name),
  };
  await vm.runInNewContext(codes[language], {
    MockApp, document,
    matchMedia: () => ({ matches: reducedMotion }),
    performance: { now: () => now },
    ResizeObserver: class { constructor(callback) { this.callback = callback; resize = this; } observe() {} disconnect() { this.disconnected = true; } },
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay, due: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  return {
    app: instance, calls, lifecycle, elements, document, timers, resize,
    requestOptions, stageSize,
    get layoutReads() { return layoutReads; },
    get imageWrites() { return imageWrites; },
    visibility(hidden) { document.hidden = hidden; listeners.get('visibilitychange')?.(); },
    event(name, value = {}) { listeners.get(name)?.(value); },
    async tick() {
      const entry = [...timers.entries()].sort((a, b) => a[1].due - b[1].due)[0];
      if (!entry) return;
      timers.delete(entry[0]); now = entry[1].due; entry[1].fn(); await flush();
    },
  };
}

test('connects before requesting only fullscreen, then polls only the app frame tool', async () => {
  const h = await harness();
  assert.equal(h.app.capabilities.availableDisplayModes.join(','), 'fullscreen');
  assert.equal(h.app.options.autoResize, false);
  assert.equal(h.lifecycle.join(','), 'connect,fullscreen');
  await h.tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].name, 'pua_screen_frame');
  assert.equal(JSON.stringify(h.calls[0].arguments), '{"after_seq":0,"last_event_id":0}');
  assert.equal([...h.timers.values()][0].delay, 250);
  assert.equal(h.requestOptions[0].timeout, 3000);
});

test('Japanese status and authentication text are confined to the selected build', async () => {
  const normal = await harness();
  const japanese = await harness({ language: 'ja' });
  assert.equal(normal.elements['empty-state-text'].textContent, '正在连接');
  assert.equal(japanese.elements['empty-state-text'].textContent, '接続しています');
  for (const [fields, defaultLabel, japaneseLabel] of [
    [{ frame_available: false }, '正在连接', '接続しています'],
    [{ frame_available: false, service_ready: false }, '未连接', '未接続'],
    [{ paused: true, pause_reason: 'device_locked' }, '等待解锁', 'ロック解除待ち'],
    [{ paused: true, pause_reason: 'authentication' }, '请完成认证', '認証を完了してください'],
    [{ paused: true }, '预览已暂停', 'プレビューは一時停止中です'],
  ]) {
    for (const h of [normal, japanese]) h.app.ontoolresult({ structuredContent: preview(fields) });
    assert.equal(normal.elements['empty-state-text'].textContent, defaultLabel);
    assert.equal(japanese.elements['empty-state-text'].textContent, japaneseLabel);
  }
  japanese.app.ontoolresult({ structuredContent: preview({ frame: frame(), frame_available: true }) });
  assert.equal(japanese.elements['live-text'].textContent, 'ライブ');
});

test('Japanese toolbar reports outcomes without changing the action contract', async () => {
  const h = await harness({ language: 'ja', reply: params => Promise.resolve(params.name === 'pua_screen_action'
    ? { structuredContent: { ok: true, action: 'home' } } : { structuredContent: preview() }) });
  h.elements['tool-home'].click();
  await flush();
  assert.equal(JSON.stringify(h.calls[0]), '{"name":"pua_screen_action","arguments":{"action":"home"}}');
  assert.equal(h.elements.toast.textContent, 'ホーム画面に戻りました');
});

test('preserves full frame aspect ratio and maps gestures using the point viewport', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(12), viewport: { width: 400, height: 800 }, busy: true,
    events: [{ id: 1, kind: 'tap', at: 1000, point: { x: 100, y: 200 }, viewport: { width: 400, height: 800 } }] }) });
  assert.equal(h.elements.app.dataset.frameSeq, '12');
  assert.equal(h.elements.app.dataset.busy, 'true');
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.screen.hidden, false);
  const bezel = parseFloat(h.elements.device.style['--bezel']);
  const width = parseFloat(h.elements.device.style.width);
  const height = parseFloat(h.elements.device.style.height);
  assert.ok(Math.abs(width - 360.96) < 1e-8);
  assert.ok(Math.abs((width - 2 * bezel) / (height - 2 * bezel) - .5) < 1e-8);
  assert.ok(height <= 776);
  assert.equal(h.elements.device.dataset.orientation, 'portrait');
  // The edge light is masked by one picture with the screen's own size and corner radius.
  const mask = decodeURIComponent(h.elements.device.style['--glow-mask']);
  const screenWidth = Math.round((width - 2 * bezel) * 100) / 100;
  assert.ok(mask.startsWith('url("data:image/svg+xml,<svg'));
  assert.ok(mask.includes(`width='${screenWidth}'`) && mask.includes(`rx='${Math.round(parseFloat(h.elements.device.style['--screen-radius']) * 100) / 100}'`));
  assert.ok(mask.includes('feGaussianBlur'));
  assert.equal(h.elements.image.src, 'data:image/png;base64,test-image-only');
  assert.equal(h.elements.cursor.style.left, '25%');
  assert.equal(h.elements.cursor.style.top, '25%');
  assert.equal(h.elements.cursor.animation.options.duration, 1600);
  h.app.ontoolresult({ structuredContent: preview({ events: [{ id: 2, kind: 'drag', at: 1000, from: { x: 40, y: 80 }, to: { x: 360, y: 720 }, duration_ms: 650 }] }) });
  const drag = h.elements.screen.children[0];
  assert.equal(drag.style.left, '10%');
  assert.equal(drag.animation.frames[3].transform, 'translate(320px, 640px) scale(.92)');
  assert.equal(drag.animation.options.duration, 1650);
});

test('tap feedback stays readable for one to two seconds with a small press and rebound', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(), viewport: { width: 400, height: 800 },
    events: [{ id: 1, kind: 'tap', at: 1000, point: { x: 200, y: 400 } }] }) });
  const effect = h.elements.cursor.animation;
  assert.ok(effect.options.duration >= 1000 && effect.options.duration <= 2000);
  const visible = effect.frames.filter(frame => frame.opacity === 1);
  assert.ok((visible.at(-1).offset - visible[0].offset) * effect.options.duration >= 1200);
  const scales = effect.frames.map(frame => Number(frame.transform.match(/scale\(([^)]+)\)/)[1]));
  assert.ok(Math.min(...scales) >= .85 && Math.min(...scales) < 1);
  assert.ok(Math.max(...scales) > 1 && Math.max(...scales) <= 1.1);
  effect.onfinish();
  assert.equal(h.elements.cursor.hidden, true);
});

test('overlapping taps and short drags keep independent readable lifetimes', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(), viewport: { width: 400, height: 800 }, events: [
    { id: 1, kind: 'tap', at: 1000, point: { x: 100, y: 200 } },
    { id: 2, kind: 'drag', at: 1000, from: { x: 200, y: 400 }, to: { x: 200, y: 600 }, duration_ms: 120 },
  ] }) });
  const tap = h.elements.cursor.animation;
  const drag = h.elements.screen.children[0];
  assert.equal(tap.cancelled, false);
  assert.ok(drag.animation.options.duration >= 1600 && drag.animation.options.duration <= 2000);
  assert.equal(drag.animation.frames.at(-2).opacity, 1);
  tap.onfinish();
  assert.equal(drag.hidden, false);
  assert.equal(drag.animation.cancelled, false);
  drag.animation.onfinish();
  assert.equal(h.elements.screen.children.length, 0);
});

test('authentication pause removes every active gesture and its animation', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(), viewport: { width: 400, height: 800 }, events: [
    { id: 1, kind: 'tap', at: 1000, point: { x: 100, y: 200 } },
    { id: 2, kind: 'tap', at: 1000, point: { x: 200, y: 400 } },
  ] }) });
  const first = h.elements.cursor.animation;
  const second = h.elements.screen.children[0].animation;
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  assert.equal(first.cancelled, true);
  assert.equal(second.cancelled, true);
  assert.equal(h.elements.cursor.hidden, true);
  assert.equal(h.elements.screen.children.length, 0);
  first.onfinish(); second.onfinish();
  assert.equal(h.elements.cursor.hidden, true);
});

test('omitted frames preserve the displayed image and acknowledgements advance once', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(7), viewport: { width: 400, height: 800 },
    events: [{ id: 3, kind: 'tap', at: 1000, point: { x: 100, y: 200 } }] }) });
  const animation = h.elements.cursor.animation;
  h.app.ontoolresult({ structuredContent: preview({ events: [{ id: 3, kind: 'tap', at: 1000, point: { x: 200, y: 400 } }] }) });
  assert.equal(h.elements.cursor.animation, animation);
  assert.equal(h.elements.image.src, 'data:image/png;base64,test-image-only');
  await h.tick();
  assert.equal(JSON.stringify(h.calls[0].arguments), '{"after_seq":7,"last_event_id":3}');
});

test('operation glow survives tool gaps, hidden panels and temporary missing frames', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'one', frame: frame(), frame_available: true }) });
  assert.equal(h.elements.app.dataset.operating, 'false');
  h.app.ontoolresult({ structuredContent: preview({ busy: true }) });
  for (let i = 0; i < 50; i++) h.app.ontoolresult({ structuredContent: preview({ busy: false }) });
  assert.equal(h.elements.app.dataset.busy, 'false');
  assert.equal(h.elements.app.dataset.operating, 'true');
  h.visibility(true);
  h.visibility(false);
  assert.equal(h.elements.app.dataset.operating, 'true');
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.app.dataset.operating, 'true');
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(2), frame_available: true }) });
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.app.dataset.operating, 'true');
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  assert.equal(h.elements.app.dataset.operating, 'false');
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(3), busy: false }) });
  assert.equal(h.elements.app.dataset.operating, 'false');
  h.app.ontoolresult({ structuredContent: preview({ busy: true }) });
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'two', frame: frame(), busy: false }) });
  assert.equal(h.elements.app.dataset.operating, 'false');
});

test('fits the entire chassis in narrow and landscape panels while preserving image coordinates', async () => {
  const h = await harness();
  let seq = 0;
  for (const [panelWidth, panelHeight, imageWidth, imageHeight] of [[170, 450, 440, 956], [900, 270, 956, 440], [310, 140, 440, 956]]) {
    h.stageSize.width = panelWidth - 24;
    h.stageSize.height = panelHeight - 24;
    h.app.ontoolresult({ structuredContent: preview({ frame: { ...frame(++seq), width: imageWidth, height: imageHeight } }) });
    h.resize.callback();
    const bezel = parseFloat(h.elements.device.style['--bezel']);
    const width = parseFloat(h.elements.device.style.width);
    const height = parseFloat(h.elements.device.style.height);
    assert.ok(width <= .96 * (panelWidth - 24) + 1e-8);
    assert.ok(height <= .96 * (panelHeight - 24) + 1e-8);
    assert.ok(Math.abs((width - 2 * bezel) / (height - 2 * bezel) - imageWidth / imageHeight) < 1e-8);
    assert.equal(h.elements.device.dataset.orientation, imageWidth > imageHeight ? 'landscape' : 'portrait');
    assert.equal(h.elements.screen.style.width, undefined);
    assert.equal(h.elements.screen.style.height, undefined);
  }
});

test('a hidden panel stops polling, and slow calls never overlap', async () => {
  let resolve;
  const h = await harness({ reply: () => new Promise(done => { resolve = done; }) });
  await h.tick();
  assert.equal(h.calls.length, 1);
  h.app.ontoolresult({ structuredContent: preview() });
  await h.tick();
  assert.equal(h.calls.length, 1);
  h.visibility(true);
  resolve({ structuredContent: preview({ frame: frame(1) }) });
  await flush();
  assert.equal(h.timers.size, 0);
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements['empty-state'].hidden, false);
  h.visibility(false);
  await h.tick();
  assert.equal(h.calls.length, 2);
});

test('paused clears the screen, errors back off silently, teardown cancels work', async () => {
  const h = await harness({ reply: async () => ({ isError: true }) });
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(4), busy: true }) });
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements.image.hidden, true);
  assert.equal(h.elements['empty-state-text'].textContent, '预览已暂停');
  assert.equal(h.elements.image.src, undefined);
  assert.equal(h.elements.app.dataset.busy, 'false');
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.app.dataset.operating, 'false');
  await h.tick();
  assert.equal([...h.timers.values()][0].delay, 500);
  await h.tick();
  assert.equal([...h.timers.values()][0].delay, 1000);
  await h.tick(); await h.tick(); await h.tick();
  assert.equal([...h.timers.values()][0].delay, 2000);
  await h.app.onteardown();
  assert.equal(h.timers.size, 0);
  assert.equal(h.resize.disconnected, true);
  assert.equal(h.elements.screen.hidden, true);
});

test('reduced motion shows a stationary cursor and old gestures are not replayed', async () => {
  const h = await harness({ reducedMotion: true, context: { displayMode: 'fullscreen', availableDisplayModes: ['fullscreen'] } });
  assert.equal(h.lifecycle.join(','), 'connect');
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(2), viewport: { width: 400, height: 800 },
    events: [{ id: 1, kind: 'drag', at: 1000, from: { x: 40, y: 80 }, to: { x: 360, y: 720 }, duration_ms: 900 }] }) });
  assert.equal(h.elements.cursor.style.left, '90%');
  assert.equal(h.elements.cursor.animation, undefined);
  h.app.ontoolresult({ structuredContent: preview({ server_time: 10000, events: [{ id: 2, kind: 'tap', at: 1000, point: { x: 200, y: 400 } }] }) });
  assert.equal(h.elements.cursor.style.left, '90%');
  for (let i = 0; i < 5; i++) await h.tick();
  assert.equal(h.elements.cursor.hidden, false);
  for (let i = 0; i < 4; i++) await h.tick();
  assert.equal(h.elements.cursor.hidden, true);
});

test('a reconnected server restarts acknowledgements and unavailable streams retain the last pixels', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'old', frame: frame(800), frame_available: true }) });
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'new', frame_available: false }) });
  assert.equal(h.elements.screen.hidden, false);
  await h.tick();
  assert.equal(h.calls[0].arguments.after_seq, 0);
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'new', frame: frame(1), frame_available: true }) });
  assert.equal(h.elements.app.dataset.frameSeq, '1');
  assert.equal(h.elements.screen.hidden, false);
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'new', frame_available: false }) });
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements.image.src, 'data:image/png;base64,test-image-only');
  assert.equal(h.elements.app.dataset.frameSeq, '1');
});

test('failed frame calls retain the previously visible image and back off', async () => {
  const h = await harness({ reply: async () => { throw new Error('disconnected'); } });
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(19), busy: true }) });
  assert.equal(h.elements.screen.hidden, false);
  await h.tick();
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements.image.src, 'data:image/png;base64,test-image-only');
  assert.equal(h.elements.app.dataset.busy, 'false');
  assert.equal(h.elements.app.dataset.operating, 'true');
  assert.equal([...h.timers.values()][0].delay, 500);
});

test('page-cache suspension retains pixels, stops work, and resumes on pageshow', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(9), busy: true }) });
  h.event('pagehide', { persisted: true });
  assert.equal(h.elements.app.dataset.pageVisible, 'false');
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.timers.size, 0);
  h.event('pageshow', { persisted: true });
  assert.equal(h.elements.app.dataset.pageVisible, 'true');
  await h.tick();
  assert.equal(h.calls[0].arguments.after_seq, 9);
  h.event('pagehide', { persisted: false });
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.timers.size, 0);
  h.event('focus');
  await h.tick();
  assert.equal(h.calls.length, 2);
  h.event('pagehide', { persisted: false });
  h.visibility(true);
  h.visibility(false);
  await h.tick();
  assert.equal(h.calls.length, 3);
  await h.app.onteardown();
  assert.equal(h.elements.screen.hidden, true);
});

test('transport close retains pixels and reconnects without another fullscreen request', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(8) }) });
  h.app.onclose();
  assert.equal(h.elements.screen.hidden, false);
  await h.tick();
  assert.equal(h.lifecycle.join(','), 'connect,fullscreen,connect');
  assert.equal(h.calls[0].arguments.after_seq, 8);
  await h.app.onteardown();
  h.app.onclose();
  assert.equal(h.timers.size, 0);
});

test('an initial host handshake failure retries instead of permanently disposing the view', async () => {
  let connects = 0;
  const h = await harness({ connectReply: async () => { if (++connects === 1) throw Error('host asleep'); } });
  assert.equal(h.calls.length, 0);
  assert.equal([...h.timers.values()][0].delay, 1000);
  await h.tick();
  assert.equal(connects, 2);
  assert.equal(h.calls.length, 1);
  assert.notEqual(h.resize.disconnected, true);
});

test('same-size frames avoid repeated layout reads and duplicate sequences avoid image writes', async () => {
  const h = await harness();
  const initialLayouts = h.layoutReads; // One initial layout for the disconnected chassis.
  for (let seq = 1; seq <= 30; seq++) h.app.ontoolresult({ structuredContent: preview({ frame: frame(seq) }) });
  assert.equal(h.layoutReads, initialLayouts + 1);
  assert.equal(h.imageWrites, 30);
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(30) }) });
  assert.equal(h.imageWrites, 30);
  h.resize.callback();
  assert.equal(h.layoutReads, initialLayouts + 2);
});

test('startup waits for the first frame with a connecting placeholder', async () => {
  const h = await harness();
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements.image.hidden, true);
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements.app.dataset.live, 'connecting');
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  assert.equal(h.elements['empty-state-text'].textContent, '正在连接');
  assert.equal(h.elements['empty-state'].hidden, false);
  assert.ok(parseFloat(h.elements.device.style.height) <= h.stageSize.height);
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1), frame_available: true }) });
  assert.equal(h.elements['empty-state'].hidden, true);
  assert.equal(h.elements.image.hidden, false);
  assert.equal(h.elements.app.dataset.live, 'live');
  // A transient gap retains actual pixels; the placeholder must not replace them.
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements['empty-state'].hidden, true);
  assert.equal(h.elements.image.hidden, false);
  assert.equal(h.elements.app.dataset.live, 'offline');
});

test('startup poll failures keep loading until a frame arrives', async () => {
  let available = false;
  const h = await harness({ reply: async () => available
    ? { structuredContent: preview({ frame: frame(1), frame_available: true }) }
    : { isError: true } });
  for (let i = 0; i < 5; i++) await h.tick();
  assert.equal(h.elements.app.dataset.live, 'connecting');
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  available = true;
  await h.tick();
  assert.equal(h.elements.app.dataset.live, 'live');
  assert.equal(h.elements['empty-state'].hidden, true);
});

test('host connection retries keep the startup loading placeholder', async () => {
  let connected = false;
  const h = await harness({ connectReply: async () => {
    if (!connected) throw new Error('host is starting');
  } });
  for (let i = 0; i < 3; i++) await h.tick();
  assert.equal(h.elements.app.dataset.live, 'connecting');
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  connected = true;
  await h.tick();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1), frame_available: true }) });
  assert.equal(h.elements.app.dataset.live, 'live');
});

test('empty screen explains connecting, offline, locked, authentication and paused states', async () => {
  const h = await harness();
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  assert.equal(h.elements['empty-state-text'].textContent, '正在连接');
  for (const [fields, state, label] of [
    [{ frame_available: false, service_ready: false }, 'offline', '未连接'],
    [{ paused: true, pause_reason: 'device_locked' }, 'locked', '等待解锁'],
    [{ paused: true, pause_reason: 'authentication' }, 'authentication', '请完成认证'],
    [{ paused: true, pause_reason: 'unknown' }, 'paused', '预览已暂停'],
    [{ paused: true }, 'paused', '预览已暂停'],
  ]) {
    h.app.ontoolresult({ structuredContent: preview(fields) });
    assert.equal(h.elements['empty-state'].dataset.state, state);
    assert.equal(h.elements['empty-state-text'].textContent, label);
    assert.equal(h.elements['empty-state'].hidden, false);
    assert.equal(h.elements.image.hidden, true);
  }
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1), frame_available: true }) });
  assert.equal(h.elements['empty-state'].hidden, true);
});

test('failed reconnect restores the authentication symbol and keeps phone controls disabled', async () => {
  let release;
  const h = await harness({ reply: params => params.name === 'pua_screen_action'
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve({ structuredContent: preview() }) });
  h.app.ontoolresult({ structuredContent: preview({ paused: true, pause_reason: 'authentication' }) });
  h.elements['tool-refresh'].click();
  await flush();
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  release({ isError: true, structuredContent: { error: { code: 'pua_unreachable' } } });
  await flush();
  assert.equal(h.elements['empty-state'].dataset.state, 'authentication');
  assert.equal(h.elements['empty-state-text'].textContent, '请完成认证');
  assert.equal(h.elements['tool-home'].disabled, true);
  assert.equal(h.elements['tool-screenshot'].disabled, true);
});

test('first image decode failure explains unavailable picture in the chassis', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1) }) });
  h.elements.image.onerror();
  assert.equal(h.elements.image.src, undefined);
  assert.equal(h.elements.image.hidden, true);
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements['empty-state-text'].textContent, '画面暂不可用');
  assert.equal(h.elements['empty-state'].hidden, false);
});

test('decode failure restores the last loaded image, while authentication pause erases its backup', async () => {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(6) }) });
  h.elements.image.onload();
  const previous = h.elements.image.src;
  h.app.ontoolresult({ structuredContent: preview({ frame: { ...frame(7), data: 'broken-fixture', width: 1800, height: 900 } }) });
  h.elements.image.onerror();
  assert.equal(h.elements.image.src, previous);
  assert.equal(h.elements.device.dataset.orientation, 'portrait');
  await h.tick();
  assert.equal(h.calls[0].arguments.after_seq, 0);
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  h.elements.image.onerror();
  assert.equal(h.elements.image.src, undefined);
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements['empty-state'].hidden, false);
});

test('unavailable and paused previews poll slowly, and focus wakes recovery immediately', async () => {
  const h = await harness({ reply: async () => ({ structuredContent: preview({ frame_available: false }) }) });
  await h.tick();
  assert.equal([...h.timers.values()][0].delay, 1000);
  h.event('focus');
  assert.equal([...h.timers.values()][0].delay, 0);
  await h.tick();
  h.visibility(true);
  assert.equal(h.elements.app.dataset.pageVisible, 'false');
  assert.equal(h.timers.size, 0);
});

test('a frame requested before authentication pause cannot restore cleared pixels', async () => {
  let resolve;
  const h = await harness({ reply: () => new Promise(done => { resolve = done; }) });
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(4) }) });
  await h.tick();
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  resolve({ structuredContent: preview({ frame: frame(5), paused: false }) });
  await flush();
  assert.equal(h.elements.image.src, undefined);
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements['empty-state'].hidden, false);
  await h.tick();
  resolve({ structuredContent: preview({ frame: frame(6), paused: false }) });
  await flush();
  assert.equal(h.elements.screen.hidden, false);
});

test('status pill names the phone model and follows the stream state', async () => {
  const h = await harness();
  assert.equal(h.elements.app.dataset.live, 'connecting');
  assert.equal(h.elements.model.textContent, '');
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1), frame_available: true, device: { model: 'iPhone 17 Pro Max' } }) });
  assert.equal(h.elements.model.textContent, 'iPhone 17 Pro Max');
  assert.equal(h.elements.app.dataset.live, 'live');
  assert.equal(h.elements['live-text'].textContent, 'Live');
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false, device: null }) });
  assert.equal(h.elements.app.dataset.live, 'offline');
  assert.equal(h.elements.model.textContent, 'iPhone 17 Pro Max');
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  assert.equal(h.elements.app.dataset.live, 'paused');
  assert.equal(h.elements['live-text'].textContent, '已暂停');
  assert.equal(h.elements['tool-home'].disabled, true);
  assert.equal(h.elements['tool-screenshot'].disabled, true);
  assert.equal(h.elements['tool-refresh'].disabled, false);
  h.app.ontoolresult({ structuredContent: preview({ paused: true, pause_reason: 'device_locked' }) });
  assert.equal(h.elements['live-text'].textContent, '等待解锁');
  assert.match(h.elements['live-text'].title, /刷新/);
  assert.equal(h.elements.image.src, undefined);
  h.app.ontoolresult({ structuredContent: preview({ paused: true, pause_reason: 'authentication' }) });
  assert.equal(h.elements['live-text'].textContent, '已暂停');
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(2), frame_available: true }) });
  assert.equal(h.elements.app.dataset.live, 'live');
  assert.equal(h.elements['tool-home'].disabled, false);
  assert.equal(h.elements['live-text'].title, '');
});

test('toolbar sends exactly one app-only action and reports its outcome', async () => {
  let release;
  const h = await harness({ reply: params => params.name === 'pua_screen_action'
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve({ structuredContent: preview() }) });
  h.elements['tool-home'].click();
  h.elements['tool-screenshot'].click();
  await flush();
  const actions = h.calls.filter(call => call.name === 'pua_screen_action');
  assert.equal(JSON.stringify(actions), '[{"name":"pua_screen_action","arguments":{"action":"home"}}]');
  assert.equal(h.requestOptions[h.calls.indexOf(actions[0])].timeout, 12000);
  assert.equal(h.elements['tool-home'].dataset.busy, 'true');
  assert.ok(['tool-refresh', 'tool-home', 'tool-screenshot'].every(id => h.elements[id].disabled));
  release({ structuredContent: { ok: true, action: 'home' } });
  await flush();
  assert.equal(h.elements.toast.textContent, '已回到主屏幕');
  assert.equal(h.elements.toast.hidden, false);
  assert.equal(h.elements.toast.dataset.tone, 'ok');
  assert.equal(h.elements['tool-home'].dataset.busy, undefined);
  assert.ok(['tool-refresh', 'tool-home', 'tool-screenshot'].every(id => !h.elements[id].disabled));
});

test('toolbar failures name the reason without changing the preview', async () => {
  const replies = [
    { isError: true, structuredContent: { error: { code: 'device_busy' } } },
    { isError: true, structuredContent: { error: { code: 'something_new' } } },
    { structuredContent: { ok: true, action: 'screenshot', copied: true } },
  ];
  const h = await harness({ reply: params => params.name === 'pua_screen_action'
    ? (replies.length ? Promise.resolve(replies.shift()) : Promise.reject(new Error('lost')))
    : Promise.resolve({ structuredContent: preview() }) });
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(4), frame_available: true }) });
  for (const [expected, tone] of [['手机正在执行操作，请稍后再试', 'error'], ['操作未完成，请重试', 'error'],
    ['截图已复制到剪贴板', 'ok'], ['操作未完成，请重试', 'error']]) {
    h.elements['tool-screenshot'].click();
    await flush();
    assert.equal(h.elements.toast.textContent, expected);
    assert.equal(h.elements.toast.dataset.tone, tone);
  }
  assert.equal(h.elements.app.dataset.frameSeq, '4');
  assert.equal(h.elements.device.hidden, false);
});

test('refresh adopts the new stream and asks for a frame at once', async () => {
  const h = await harness({ reply: params => Promise.resolve(params.name === 'pua_screen_action'
    ? { structuredContent: { ok: true, action: 'refresh', service_ready: true, ...preview({ stream_id: 'next', frame_available: false }) } }
    : { structuredContent: preview({ stream_id: 'first' }) }) });
  h.app.ontoolresult({ structuredContent: preview({ stream_id: 'first', frame: frame(9), frame_available: true }) });
  assert.equal(h.elements.app.dataset.frameSeq, '9');
  h.elements['tool-refresh'].click();
  await flush();
  assert.equal(h.elements.toast.textContent, '已刷新连接');
  assert.equal([...h.timers.values()].some(timer => timer.delay === 0), true);
  await h.tick();
  const polls = h.calls.filter(call => call.name === 'pua_screen_frame');
  assert.equal(polls.at(-1).arguments.after_seq, 0);
  // The last pixels stay until the new stream delivers its first frame.
  assert.equal(h.elements.device.hidden, false);
});

test('refresh reports a real disconnected service instead of a success toast', async () => {
  const h = await harness({ reply: () => Promise.resolve({ structuredContent: { ok: true, service_ready: false, ...preview({ frame_available: false }) } }) });
  h.elements['tool-refresh'].click();
  await flush();
  assert.match(h.elements.toast.textContent, /未连接到手机/);
  assert.equal(h.elements.toast.dataset.tone, 'error');
  assert.equal(h.elements['empty-state-text'].textContent, '未连接');
  assert.equal(h.elements.device.hidden, false);
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements['empty-state'].dataset.state, 'offline');
});

test('a poll from before user reconnect cannot put the recovered preview back in pause', async () => {
  let release;
  const h = await harness({ reply: params => params.name === 'pua_screen_frame'
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve({ structuredContent: { ok: true, service_ready: true, ...preview({ stream_id: 'reconnected', frame_available: false }) } }) });
  h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
  await h.tick();
  h.elements['tool-refresh'].click();
  await flush();
  release({ structuredContent: preview({ paused: true, pause_reason: 'unknown' }) });
  await flush();
  assert.equal(h.elements.app.dataset.live, 'connecting');
  assert.equal(h.elements['empty-state-text'].textContent, '正在连接');
  assert.equal(h.calls.filter(call => call.name === 'pua_screen_action').length, 1);
});

test('host theme is applied and followed, and teardown releases the toolbar', async () => {
  const h = await harness({ context: { displayMode: 'fullscreen', availableDisplayModes: ['fullscreen'], theme: 'dark' } });
  assert.equal(h.document.documentElement.dataset.theme, 'dark');
  h.app.onhostcontextchanged({ theme: 'light' });
  assert.equal(h.document.documentElement.dataset.theme, 'light');
  h.app.onhostcontextchanged({ theme: 'sepia' });
  assert.equal(h.document.documentElement.dataset.theme, 'light');
  h.elements['tool-home'].click();
  await flush();
  await h.app.onteardown();
  assert.equal(h.elements.toast.hidden, true);
  assert.equal(h.elements['tool-home'].handlers.size, 0);
  const before = h.calls.length;
  h.elements['tool-home'].click();
  await flush();
  assert.equal(h.calls.length, before);
});
