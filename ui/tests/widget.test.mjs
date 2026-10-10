import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { transform } from 'esbuild';

const source = await readFile(new URL('../src/app.ts', import.meta.url), 'utf8');
const { code } = await transform(`(async () => {
${source.replace("import { App } from '@modelcontextprotocol/ext-apps';", 'const App = globalThis.MockApp;')}
})()`, { loader: 'ts', target: 'es2022' });

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const preview = (fields = {}) => ({ server_time: 1000, frame_age_ms: 0, frame: null, viewport: null, busy: false, paused: false, events: [], ...fields });
// Fixtures exist only in this isolated DOM test, never in the shipped widget.
const frame = (seq = 1) => ({ seq, data: 'test-image-only', mimeType: 'image/png', width: 900, height: 1800 });

async function harness({ reducedMotion = false, context = { displayMode: 'inline', availableDisplayModes: ['fullscreen'] }, reply, connectReply } = {}) {
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
    getBoundingClientRect() { return { left: 10, top: 20, width: 200, height: 400 }; },
    setPointerCapture(id) { this.captured = id; },
    hasPointerCapture(id) { return this.captured === id; },
    releasePointerCapture() { this.captured = undefined; },
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
  await vm.runInNewContext(code, {
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
    advance(ms) { now += ms; },
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

test('no frame keeps a fitted black-screen chassis and disconnected label', async () => {
  const h = await harness();
  assert.equal(h.elements.device.hidden, false);
  assert.equal(h.elements.screen.hidden, false);
  assert.equal(h.elements.image.hidden, true);
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements['empty-state-text'].textContent, '未连接');
  assert.equal(h.elements['empty-state'].hidden, false);
  assert.ok(parseFloat(h.elements.device.style.height) <= h.stageSize.height);
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(1), frame_available: true }) });
  assert.equal(h.elements['empty-state'].hidden, true);
  assert.equal(h.elements.image.hidden, false);
  // A transient gap retains actual pixels; the placeholder must not replace them.
  h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
  assert.equal(h.elements['empty-state'].hidden, true);
  assert.equal(h.elements.image.hidden, false);
});

test('empty screen explains connecting, offline, locked, authentication and paused states', async () => {
  const h = await harness();
  assert.equal(h.elements['empty-state'].dataset.state, 'connecting');
  assert.equal(h.elements['empty-state-text'].textContent, '正在连接');
  for (const [fields, state, label] of [
    [{ frame_available: false }, 'offline', '未连接'],
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
  assert.equal(h.elements.app.dataset.live, undefined);
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
  assert.equal(h.elements.app.dataset.live, 'offline');
  assert.equal(h.elements['empty-state-text'].textContent, '未连接');
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

function pointerEvent(x, y, extra = {}) {
  return { clientX: x, clientY: y, pointerId: 1, isPrimary: true, button: 0, preventDefault() {}, ...extra };
}
async function livePointerHarness() {
  const h = await harness();
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(), viewport: { width: 400, height: 800 } }) });
  h.elements.image.onload();
  h.pointer = (name, x, y, extra) => h.elements.screen.handlers.get(name)?.(pointerEvent(x, y, extra));
  return h;
}
test('pointer click and drag map scaled screen coordinates and dispatch once', async () => {
  const h = await livePointerHarness();
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
  const taps = h.calls.filter(c => c.name === 'pua_screen_action');
  assert.equal(JSON.stringify(taps[0].arguments), JSON.stringify({ action: 'tap', x: 100, y: 200, width: 400, height: 800 }));
  h.app.ontoolresult({structuredContent: preview({frame_available:true})});
  h.elements.image.onload();
  h.pointer('pointerdown', 110, 320); h.pointer('pointerup', 110, 120); await flush();
  const drag = h.calls.filter(c => c.name === 'pua_screen_action')[1].arguments;
  assert.equal(drag.action, 'drag'); assert.equal(drag.y, 600); assert.equal(drag.to_y, 200);
});
test('pointer cancellation, secondary buttons, outside release and pause send nothing', async () => {
  for (const scenario of ['cancel', 'outside', 'right', 'pause', 'rotate', 'hidden', 'multi']) {
    const h = await livePointerHarness();
    h.pointer('pointerdown', 60, 120, scenario === 'right' ? { button: 2 } : {});
    if (scenario === 'cancel') h.pointer('pointercancel', 60, 120);
    if (scenario === 'multi') h.pointer('pointerdown', 60, 120, { isPrimary: false, pointerId: 2 });
    if (scenario === 'pause') h.app.ontoolresult({ structuredContent: preview({ paused: true }) });
    if (scenario === 'rotate') h.app.ontoolresult({ structuredContent: preview({ viewport: { width: 800, height: 400 } }) });
    if (scenario === 'hidden') h.visibility(true);
    h.pointer('pointerup', scenario === 'outside' ? 400 : 60, 120); await flush();
    assert.equal(h.calls.filter(c => c.name === 'pua_screen_action').length, 0, scenario);
  }
});
test('pointer input requires a decoded live frame and cannot queue during a mutation', async () => {
  const h = await livePointerHarness();
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120);
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
  assert.equal(h.calls.filter(c => c.name === 'pua_screen_action').length, 1);
});

test('letterboxed margins are excluded and resized image coordinates stay accurate', async () => {
  const h = await livePointerHarness();
  h.elements.screen.getBoundingClientRect = () => ({ left: 10, top: 20, width: 400, height: 400 });
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
  assert.equal(h.calls.filter(c => c.name === 'pua_screen_action').length, 0);
  h.pointer('pointerdown', 160, 120); h.pointer('pointerup', 160, 120); await flush();
  const tap = h.calls.find(c => c.name === 'pua_screen_action').arguments;
  assert.equal(tap.x, 100); assert.equal(tap.y, 200);
});
test('a failed image decode and busy preview reject pointer input', async () => {
  for (const scenario of ['decode', 'busy', 'offline']) {
    const h = await livePointerHarness();
    if (scenario === 'decode') { h.elements.image.onerror(); h.elements.image.onload(); }
    if (scenario === 'busy') h.app.ontoolresult({ structuredContent: preview({ busy: true }) });
    if (scenario === 'offline') h.app.ontoolresult({ structuredContent: preview({ frame_available: false }) });
    h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
    assert.equal(h.calls.filter(c => c.name === 'pua_screen_action').length, 0, scenario);
  }
});
test('mutation failure warns once without automatic replay', async () => {
  const h = await harness({reply: params => params.name === 'pua_screen_action'
    ? Promise.reject(new Error('timeout')) : Promise.resolve({structuredContent:preview()})});
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(), viewport: { width: 400, height: 800 } }) });
  h.elements.image.onload();
  h.elements.screen.handlers.get('pointerdown')(pointerEvent(60,120));
  h.elements.screen.handlers.get('pointerup')(pointerEvent(60,120)); await flush();
  assert.match(h.elements.toast.textContent, /结果不确定/);
  await h.tick(); await h.tick();
  assert.equal(h.calls.filter(c => c.name === 'pua_screen_action').length, 1);
});

test('static fresh stream remains interactive without decoding duplicate frames', async () => {
  const h = await livePointerHarness();
  h.app.ontoolresult({structuredContent:preview({frame_available:true,frame_age_ms:50})});
  h.pointer('pointerdown',60,120); h.pointer('pointerup',60,120); await flush();
  assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,1);
});
test('retained stale stream and missing heartbeat metadata block input', async () => {
  for (const age of [2500, null, undefined]) {
    const h = await livePointerHarness();
    h.app.ontoolresult({structuredContent:preview({frame_available:true,frame_age_ms:age})});
    h.pointer('pointerdown',60,120); h.pointer('pointerup',60,120); await flush();
    assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,0);
  }
});


test('cosmetic busy afterglow does not block input, but an active operation does', async () => {
  for (const input_busy of [false, true]) {
    const h = await livePointerHarness();
    h.app.ontoolresult({structuredContent:preview({busy:true,input_busy})});
    h.pointer('pointerdown',60,120); h.pointer('pointerup',60,120); await flush();
    assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,input_busy ? 0 : 1);
  }
});
test('completed gesture replaces the pending poll with an immediate refresh', async () => {
  const h = await livePointerHarness();
  await h.tick();
  h.pointer('pointerdown',60,120); h.pointer('pointerup',60,120); await flush();
  assert.equal(Math.min(...[...h.timers.values()].map(t=>t.delay)),0);
});

function wheelEvent(deltaX, deltaY, extra = {}) {
  return { clientX:110, clientY:220, deltaX, deltaY, deltaMode:0,
    preventDefault() { this.prevented = true; }, ...extra };
}
test('trackpad wheel maps all four directions to bounded centered swipes', async () => {
  for (const [dx,dy] of [[0,60],[0,-60],[60,0],[-60,0]]) {
    const h = await livePointerHarness();
    const event = wheelEvent(dx,dy);
    h.elements.screen.handlers.get('wheel')(event); await flush();
    const args = h.calls.find(c=>c.name==='pua_screen_action').arguments;
    assert.equal(args.action,'drag'); assert.equal(event.prevented,true);
    assert.equal(Math.sign(args.to_x-args.x), dx ? -Math.sign(dx) : 0);
    assert.equal(Math.sign(args.to_y-args.y), dy ? -Math.sign(dy) : 0);
    assert.ok(args.x>0 && args.to_x<400 && args.y>0 && args.to_y<800);
  }
});
test('wheel accumulates small deltas and consumes momentum even after action completes', async () => {
  const h = await livePointerHarness();
  const wheel = h.elements.screen.handlers.get('wheel');
  wheel(wheelEvent(0,10)); wheel(wheelEvent(0,10));
  assert.equal(h.calls.length,0);
  wheel(wheelEvent(0,10)); await flush();
  h.app.ontoolresult({structuredContent:preview({frame_available:true})});
  for(let i=0;i<15;i++) { h.advance(80); wheel(wheelEvent(0,50)); }
  assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,1);
  h.advance(181); wheel(wheelEvent(0,-50)); await flush();
  assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,2);
});
test('wheel rejects pinch, stale frames, busy device, margins and pointer overlap', async () => {
  for (const scenario of ['pinch','stale','busy','margin','pointer','hidden']) {
    const h=await livePointerHarness();
    if(scenario==='stale') h.advance(2500);
    if(scenario==='busy') h.app.ontoolresult({structuredContent:preview({input_busy:true})});
    if(scenario==='pointer') h.pointer('pointerdown',110,220);
    if(scenario==='hidden') h.visibility(true);
    h.elements.screen.handlers.get('wheel')(wheelEvent(0,100,{
      ctrlKey:scenario==='pinch',clientX:scenario==='margin'?500:110})); await flush();
    assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,0,scenario);
  }
});
test('wheel normalizes line/page input and removes listener on teardown', async () => {
  for(const mode of [1,2]) {
    const h=await livePointerHarness();
    h.elements.screen.handlers.get('wheel')(wheelEvent(0,2,{deltaMode:mode})); await flush();
    assert.equal(h.calls.filter(c=>c.name==='pua_screen_action').length,1);
    await h.app.onteardown();
    assert.equal(h.elements.screen.handlers.has('wheel'),false);
  }
});

test('new frames reject input until their own load, including late loads from replaced frames', async () => {
  const h = await livePointerHarness();
  const oldLoad = h.elements.image.onload;
  h.app.ontoolresult({ structuredContent: preview({ frame: frame(2) }) });
  oldLoad(); // Even the same data URL belongs to a different frame generation.
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120);
  h.elements.screen.handlers.get('wheel')({ ...pointerEvent(110,220), deltaY: 80, deltaX: 0, deltaMode: 0 });
  await flush();
  assert.equal(h.calls.length, 0);
  h.elements.image.onload();
  h.pointer('pointerdown', 60, 120); h.pointer('pointerup', 60, 120); await flush();
  assert.equal(h.calls.filter(c => c.arguments.action === 'tap').length, 1);
});

test('polling recovers rotated viewport and restores input without another phone tool', async () => {
  let resolveViewport;
  const h = await harness({ reply: params => params.arguments.action === 'viewport'
    ? new Promise(resolve => { resolveViewport = resolve; })
    : Promise.resolve({ structuredContent: preview({ frame_available: true,
        frame: { ...frame(2), width: 1800, height: 900 }, viewport: { width: 400, height: 800 } }) }) });
  await h.tick();
  h.elements.image.onload();
  const click = () => {
    h.elements.screen.handlers.get('pointerdown')(pointerEvent(110,220));
    h.elements.screen.handlers.get('pointerup')(pointerEvent(110,220));
  };
  click(); await flush();
  assert.equal(h.calls.filter(c => c.arguments.action === 'tap').length, 0);
  resolveViewport({ structuredContent: { viewport: { width: 800, height: 400 } } });
  await flush(); click(); await flush();
  const tap = h.calls.find(c => c.arguments.action === 'tap');
  assert.equal(tap.arguments.width, 800);
  assert.equal(tap.arguments.height, 400);
  assert.equal(tap.arguments.x, 400);
  assert.equal(tap.arguments.y, 200);
});

test('viewport recovery failures are throttled while frame polling continues', async () => {
  const h = await harness({ reply: params => Promise.resolve(params.arguments.action === 'viewport'
    ? { isError: true }
    : { structuredContent: preview({ frame_available: true,
        frame: { ...frame(), width: 1800, height: 900 }, viewport: { width: 400, height: 800 } }) }) });
  await h.tick();
  for (let i = 0; i < 7; i++) await h.tick();
  assert.equal(h.calls.filter(c => c.arguments.action === 'viewport').length, 1);
  assert.equal(h.calls.filter(c => c.name === 'pua_screen_frame').length, 8);
  await h.tick();
  assert.equal(h.calls.filter(c => c.arguments.action === 'viewport').length, 2);
});
