import { App } from '@modelcontextprotocol/ext-apps';

type Size = { width: number; height: number };
type Point = { x: number; y: number };
type Frame = Size & { seq: number; data: string; mimeType: string };
type Gesture = {
  id: number;
  kind: 'tap' | 'drag';
  at: number;
  point?: Point;
  from?: Point;
  to?: Point;
  duration_ms?: number;
  viewport?: Size;
};
type Preview = {
  server_time: number;
  stream_id?: string;
  frame: Frame | null;
  frame_available?: boolean;
  frame_age_ms?: number | null;
  viewport: Size | null;
  busy: boolean;
  input_busy?: boolean;
  paused: boolean;
  pause_reason?: 'device_locked' | 'authentication' | 'unknown' | null;
  events: Gesture[];
  device?: { model?: string } | null;
};
type LiveState = 'connecting' | 'live' | 'paused' | 'offline';
type EmptyState = 'connecting' | 'offline' | 'locked' | 'authentication' | 'paused' | 'unavailable';
type ToolName = 'refresh' | 'home' | 'screenshot';
type CursorEffect = { node: HTMLElement; animation?: Animation; timer?: ReturnType<typeof setTimeout> };

const root = document.getElementById('app')!;
const device = document.getElementById('device')!;
const screen = document.getElementById('screen')!;
const image = document.getElementById('image')! as HTMLImageElement;
const emptyState = document.getElementById('empty-state')!;
const emptyText = document.getElementById('empty-state-text')!;
const cursor = document.getElementById('cursor')!;
const stage = document.getElementById('stage')!;
const model = document.getElementById('model')!;
const liveText = document.getElementById('live-text')!;
const toast = document.getElementById('toast')!;
const tools: Record<ToolName, HTMLButtonElement> = {
  refresh: document.getElementById('tool-refresh') as HTMLButtonElement,
  home: document.getElementById('tool-home') as HTMLButtonElement,
  screenshot: document.getElementById('tool-screenshot') as HTMLButtonElement,
};
const app = new App(
  { name: 'iPhone Use Screen', version: '0.3.6' },
  { availableDisplayModes: ['fullscreen'] },
  { autoResize: false },
);
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let ready = false;
let disposed = false;
let inFlight = false;
let frameSeq = 0;
let frameGeneration = 0;
let streamId: string | undefined;
let eventId = 0;
let dimensions: Size | undefined;
let viewport: Size | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
let failures = 0;
const cursorEffects = new Set<CursorEffect>();
let requestedFullscreen = false;
let connecting: Promise<void> | undefined;
let suspended = false;
let lastGoodFrame: { source: string; size: Size } | undefined;
let acting = false;
let toastTimer: ReturnType<typeof setTimeout> | undefined;
const FRAME_INTERVAL = 250;
const REQUEST_TIMEOUT = 3000;
// A screenshot or Home may queue behind the phone operation already running.
const ACTION_TIMEOUT = 12000;
const LIVE_TEXT: Record<LiveState, string> = { connecting: '连接中', live: 'Live', paused: '已暂停', offline: '未连接' };
const EMPTY_TEXT: Record<EmptyState, string> = {
  connecting: '正在连接', offline: '未连接', locked: '等待解锁',
  authentication: '请完成认证', paused: '预览已暂停', unavailable: '画面暂不可用',
};
const DONE: Record<ToolName, string> = { refresh: '已刷新连接', home: '已回到主屏幕', screenshot: '截图已复制到剪贴板' };
const FAILED: Record<string, string> = {
  device_busy: '手机正在执行操作，请稍后再试',
  preview_paused: '认证接管期间已暂停',
  clipboard_unavailable: '截图未能写入剪贴板',
  pua_unreachable: '未连接到手机',
  phone_locked: '请先解锁手机',
  stale_viewport: '屏幕方向已变化，请等待新画面',
};

const validSize = (value: unknown): value is Size => {
  const size = value as Size | undefined;
  return !!size && Number.isFinite(size.width) && Number.isFinite(size.height)
    && size.width > 0 && size.height > 0;
};
const validPoint = (value: unknown): value is Point => {
  const point = value as Point | undefined;
  return !!point && Number.isFinite(point.x) && Number.isFinite(point.y);
};

function syncTools() {
  const paused = root.dataset.live === 'paused';
  tools.refresh.disabled = acting;
  // While the user authenticates on the phone nothing is captured or sent from here.
  tools.home.disabled = tools.screenshot.disabled = acting || paused;
}

function showEmpty(state: EmptyState) {
  emptyState.dataset.state = state;
  emptyText.textContent = EMPTY_TEXT[state];
}

function setLive(state: LiveState) {
  if (!image.src) showEmpty(state === 'paused' ? 'paused' : state === 'offline' ? 'offline' : 'connecting');
  if (root.dataset.live === state) return;
  root.dataset.live = state;
  liveText.textContent = LIVE_TEXT[state];
  liveText.title = '';
  syncTools();
}

function notify(text: string, failed = false) {
  toast.textContent = text;
  toast.dataset.tone = failed ? 'error' : 'ok';
  toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastTimer = undefined; toast.hidden = true; }, 1800);
}

function applyTheme(theme: unknown) {
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
}

function finishCursor(effect: CursorEffect) {
  if (!cursorEffects.delete(effect)) return;
  effect.animation?.cancel();
  if (effect.timer) clearTimeout(effect.timer);
  effect.node.hidden = true;
  if (effect.node !== cursor) effect.node.remove();
}

function hideCursor() {
  for (const effect of cursorEffects) finishCursor(effect);
}

function clearFrame(resetOperating = true) {
  const hadDimensions = !!dimensions;
  frameGeneration++;
  hideCursor();
  image.removeAttribute('src');
  image.hidden = true;
  emptyState.hidden = false;
  device.hidden = screen.hidden = disposed;
  dimensions = undefined;
  lastGoodFrame = undefined;
  frameSeq = 0;
  root.dataset.frameSeq = '0';
  root.dataset.busy = 'false';
  if (resetOperating) root.dataset.operating = 'false';
  if (!disposed && hadDimensions) fitFrame();
}

function retainFrame() {
  hideCursor();
  root.dataset.busy = 'false';
}

const visible = () => !disposed && !suspended && !document.hidden;

// Bake each soft ring phase once per layout. Top/bottom centers retain a little light,
// then join more strongly as the existing color and dot phases crossfade.
function glowMask(width: number, height: number, radius: number, band: number, bridge: number) {
  const round = (value: number) => Math.round(value * 100) / 100;
  const bloom = Math.min(width, height) * .27;
  const corners = [[0, 0], [width, 0], [0, height], [width, height]]
    .map(([x, y]) => `<circle cx='${round(x)}' cy='${round(y)}' r='${round(bloom)}'/>`).join('');
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='${round(width)}' height='${round(height)}'>`
    + `<defs><linearGradient id='x'><stop stop-color='#fff'/><stop offset='.16' stop-color='#fff' stop-opacity='.85'/><stop offset='.36' stop-color='#fff' stop-opacity='${bridge}'/><stop offset='.64' stop-color='#fff' stop-opacity='${bridge}'/><stop offset='.84' stop-color='#fff' stop-opacity='.85'/><stop offset='1' stop-color='#fff'/></linearGradient>`
    + `<linearGradient id='y' x2='0' y2='1'><stop stop-color='#fff'/><stop offset='.1' stop-color='#fff' stop-opacity='.85'/><stop offset='.28' stop-color='#fff' stop-opacity='0'/><stop offset='.72' stop-color='#fff' stop-opacity='0'/><stop offset='.9' stop-color='#fff' stop-opacity='.85'/><stop offset='1' stop-color='#fff'/></linearGradient>`
    + `<mask id='ends'><rect width='100%' height='100%' fill='url(#y)'/></mask><mask id='corners'><rect width='100%' height='100%' fill='url(#x)' mask='url(#ends)'/></mask></defs>`
    + `<filter id='f' x='-30%' y='-30%' width='160%' height='160%'><feGaussianBlur stdDeviation='${round(band * .3)}'/></filter>`
    + `<filter id='g' x='-60%' y='-60%' width='220%' height='220%'><feGaussianBlur stdDeviation='${round(bloom * .42)}'/></filter>`
    + `<g mask='url(#corners)'><g fill='#fff' fill-opacity='.46' filter='url(#g)'>${corners}</g>`
    + `<rect width='${round(width)}' height='${round(height)}' rx='${round(radius)}' fill='none' stroke='#fff' stroke-width='${round(band * .9)}' filter='url(#f)'/>`
    + `<rect x='.75' y='.75' width='${round(width - 1.5)}' height='${round(height - 1.5)}' rx='${round(Math.max(0, radius - .75))}' fill='none' stroke='#fff' stroke-opacity='.9' stroke-width='1.5'/></g></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

// Three seamless, static wave phases crossfade independently of the colour fields. Dot sizes
// vary across each tile without per-frame SVG generation or gradient repainting.
function glowDots(step: number, phase: number) {
  const count = 16;
  const round = (value: number) => Math.round(value * 100) / 100;
  const size = round(count * step);
  const dots: string[] = [];
  for (let row = 0; row < count; row++) {
    for (let col = 0; col < count; col++) {
      const diagonal = (col + row) / count * Math.PI * 2;
      const bend = Math.sin((col - row) / count * Math.PI * 2) * .8;
      const wave = (1 + Math.sin(diagonal + bend + phase)) / 2;
      dots.push(`<circle cx='${round((col + .5) * step)}' cy='${round((row + .5) * step)}' r='${round(step * (.04 + .23 * wave))}' fill-opacity='${round(.25 + .65 * wave)}'/>`);
    }
  }
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='${size}' height='${size}' viewBox='0 0 ${size} ${size}'><g fill='#fff'>${dots.join('')}</g></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

function fitFrame() {
  const size = dimensions ?? viewport ?? { width: 440, height: 956 };
  // The stage is what remains between the status pill and the toolbar.
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  const shortSide = Math.min(size.width, size.height);
  const bezel = shortSide * .024;
  const outerWidth = size.width + 2 * bezel;
  const outerHeight = size.height + 2 * bezel;
  const scale = .96 * Math.min(width / outerWidth, height / outerHeight);
  device.style.width = `${outerWidth * scale}px`;
  device.style.height = `${outerHeight * scale}px`;
  device.style.setProperty('--bezel', `${bezel * scale}px`);
  const radius = shortSide * .12 * scale;
  device.style.setProperty('--screen-radius', `${radius}px`);
  const band = Math.min(bezel * scale * 3.8, radius);
  for (const [phase, bridge] of [.12, .32, .62].entries()) {
    device.style.setProperty(phase === 0 ? '--glow-mask' : `--glow-mask-${phase}`,
      glowMask(size.width * scale, size.height * scale, radius, band, bridge));
  }
  const dotStep = Math.max(5, bezel * scale * .95);
  for (let phase = 0; phase < 3; phase++) {
    device.style.setProperty(`--glow-dots-${phase}`, glowDots(dotStep, phase * Math.PI * 2 / 3));
  }
  device.dataset.orientation = size.width > size.height ? 'landscape' : 'portrait';
}

function pointStyle(point: Point, size: Size) {
  return {
    left: `${Math.max(0, Math.min(1, point.x / size.width)) * 100}%`,
    top: `${Math.max(0, Math.min(1, point.y / size.height)) * 100}%`,
  };
}

function showGesture(gesture: Gesture) {
  if (screen.hidden || image.hidden) return;
  const size = validSize(gesture.viewport) ? gesture.viewport : viewport;
  if (!size) return;
  if (dimensions && Math.abs((size.width / size.height) / (dimensions.width / dimensions.height) - 1) > .08) return;
  const point = gesture.kind === 'tap' ? gesture.point : gesture.from;
  if (!validPoint(point)) return;
  if (gesture.kind === 'drag' && !validPoint(gesture.to)) return;
  // Keep each indication readable even when several real actions arrive together.
  // Overlapping effects have independent lifetimes, so a new tap cannot erase one.
  const marker = cursor.hidden ? cursor : cursor.cloneNode(false) as HTMLElement;
  if (marker !== cursor) {
    marker.removeAttribute('id');
    screen.appendChild(marker);
  }
  Object.assign(marker.style, pointStyle(point, size));
  marker.hidden = false;
  const effect: CursorEffect = { node: marker };
  cursorEffects.add(effect);
  const movement = Math.max(600, Math.min(1000, gesture.duration_ms || 650));
  const duration = gesture.kind === 'drag' ? movement + 1000 : 1600;
  if (reducedMotion.matches) {
    if (gesture.kind === 'drag') Object.assign(marker.style, pointStyle(gesture.to!, size));
    effect.timer = setTimeout(() => finishCursor(effect), duration);
    return;
  }
  const delta = gesture.kind === 'drag'
    ? `translate(${(gesture.to!.x - point.x) / size.width * screen.clientWidth}px, ${(gesture.to!.y - point.y) / size.height * screen.clientHeight}px)`
    : '';
  const frames: Keyframe[] = gesture.kind === 'drag'
    ? [
      { transform: 'translate(0, 0) scale(.96)', opacity: 0 },
      { transform: 'translate(0, 0) scale(1)', opacity: 1, offset: 80 / duration },
      { transform: 'translate(0, 0) scale(.92)', opacity: 1, offset: 200 / duration, easing: 'cubic-bezier(.3,0,.2,1)' },
      { transform: `${delta} scale(.92)`, opacity: 1, offset: (movement + 200) / duration },
      { transform: `${delta} scale(1.06)`, opacity: 1, offset: (movement + 380) / duration },
      { transform: `${delta} scale(1)`, opacity: 1, offset: (duration - 350) / duration },
      { transform: `${delta} scale(1)`, opacity: 1, offset: (duration - 200) / duration },
      { transform: `${delta} scale(.98)`, opacity: 0 },
    ]
    : [
      { transform: 'scale(.96)', opacity: 0 },
      { transform: 'scale(1)', opacity: 1, offset: .06 },
      { transform: 'scale(.9)', opacity: 1, offset: .16 },
      { transform: 'scale(1.06)', opacity: 1, offset: .28 },
      { transform: 'scale(1)', opacity: 1, offset: .4 },
      { transform: 'scale(1)', opacity: 1, offset: .875 },
      { transform: 'scale(.98)', opacity: 0 },
    ];
  effect.animation = marker.animate(frames, { duration, easing: 'linear' });
  effect.animation.onfinish = () => finishCursor(effect);
}

function consume(value: unknown) {
  if (disposed || !value || typeof value !== 'object') return;
  const preview = value as Partial<Preview>;
  if (typeof preview.stream_id === 'string' && preview.stream_id !== streamId) {
    streamId = preview.stream_id;
    // Sequence numbers restart with the server, but keep the last pixels until
    // its replacement is ready. Authentication pause remains an explicit erase.
    frameSeq = 0;
    eventId = 0;
    root.dataset.operating = 'false';
    retainFrame();
  }
  if (typeof preview.device?.model === 'string' && preview.device.model) model.textContent = preview.device.model;
  if (validSize(preview.viewport) && (viewport?.width !== preview.viewport.width || viewport?.height !== preview.viewport.height)) {
    viewport = preview.viewport;
    if (!dimensions) fitFrame();
  }
  if (preview.paused === true) {
    clearFrame();
    setLive('paused');
    liveText.textContent = preview.pause_reason === 'device_locked' ? '等待解锁' : LIVE_TEXT.paused;
    liveText.title = preview.pause_reason === 'device_locked'
      ? '解锁 iPhone 后继续任务，或点击刷新恢复预览'
      : '预览已暂停；完成手机认证后继续任务或点击刷新恢复画面';
    showEmpty(preview.pause_reason === 'device_locked' ? 'locked'
      : preview.pause_reason === 'authentication' ? 'authentication' : 'paused');
    return;
  }
  if (preview.frame_available === false) {
    retainFrame();
    setLive('offline');
  } else if (preview.frame_available === true || preview.frame) {
    setLive('live');
  }
  freshUntil = typeof preview.frame_age_ms === 'number' && preview.frame_age_ms >= 0
    ? performance.now() + Math.max(0, 2000 - preview.frame_age_ms) : -Infinity;
  const frame = preview.frame;
  if (frame && validSize(frame) && Number.isInteger(frame.seq) && frame.seq > frameSeq
      && typeof frame.data === 'string' && frame.data.length > 0
      && ['image/jpeg', 'image/png', 'image/webp'].includes(frame.mimeType)) {
    frameSeq = frame.seq;
    root.dataset.frameSeq = String(frame.seq);
    if (!dimensions || dimensions.width !== frame.width || dimensions.height !== frame.height) {
      dimensions = { width: frame.width, height: frame.height };
      fitFrame();
    }
    decodedAt = -Infinity;
    failedFrame = false;
    const source = `data:${frame.mimeType};base64,${frame.data}`;
    const generation = frameGeneration;
    image.onload = () => {
      if (disposed || generation !== frameGeneration || frame.seq !== frameSeq || image.src !== source || failedFrame) return;
      decodedAt = performance.now();
      lastGoodFrame = { source, size: { width: frame.width, height: frame.height } };
    };
    image.src = source;
    image.hidden = false;
    emptyState.hidden = true;
    device.hidden = false;
    screen.hidden = false;
  }
  if (typeof preview.busy === 'boolean') root.dataset.busy = String(preview.busy);
  root.dataset.inputBusy = String(preview.input_busy ?? preview.busy);
  // Keep the edge light on between tools and while the model plans its next action.
  if (preview.busy === true) root.dataset.operating = 'true';
  if (Array.isArray(preview.events)) {
    for (const gesture of preview.events) {
      if (!Number.isInteger(gesture.id) || gesture.id <= eventId) continue;
      eventId = gesture.id;
      const age = (preview.server_time || Date.now()) - gesture.at;
      if (Number.isFinite(age) && age >= -1000 && age <= 2500
          && (gesture.kind === 'tap' || gesture.kind === 'drag')) {
        root.dataset.operating = 'true';
        showGesture(gesture);
      }
    }
  }
}

function schedule(delay: number) {
  if (!visible() || timer) return;
  timer = setTimeout(() => {
    timer = undefined;
    void poll();
  }, delay);
}

async function poll() {
  if (!visible() || inFlight) return;
  inFlight = true;
  const started = performance.now();
  let nextDelay = FRAME_INTERVAL;
  try {
    if (!ready) await connect();
    if (!visible()) return;
    const generation = frameGeneration;
    const result = await app.callServerTool({
      name: 'pua_screen_frame',
      arguments: { after_seq: frameSeq, last_event_id: eventId },
    }, { timeout: REQUEST_TIMEOUT });
    if (!visible() || generation !== frameGeneration) return;
    if (result.isError) throw new Error('preview unavailable');
    consume(result.structuredContent);
    await recoverViewport();
    failures = 0;
    const preview = result.structuredContent as Partial<Preview> | undefined;
    nextDelay = preview?.paused || preview?.frame_available === false
      ? 1000 : Math.max(0, FRAME_INTERVAL - (performance.now() - started));
  } catch {
    failures += 1;
    nextDelay = Math.min(2000, 500 * failures);
    retainFrame();
    if (failures > 1 && root.dataset.live !== 'paused') setLive('offline');
  } finally {
    inFlight = false;
    schedule(nextDelay);
  }
}

async function act(name: ToolName) {
  if (acting || disposed || tools[name].disabled) return;
  const previousEmptyState = emptyState.dataset.state as EmptyState;
  if (name === 'refresh') {
    frameGeneration++; // Ignore a poll from before this reconnect click.
    if (!image.src) showEmpty('connecting');
  }
  acting = true;
  tools[name].dataset.busy = 'true';
  syncTools();
  try {
    if (!ready) await connect();
    const result = await app.callServerTool({
      name: 'pua_screen_action',
      arguments: { action: name },
    }, { timeout: ACTION_TIMEOUT });
    const data = result.structuredContent as (Partial<Preview> & { service_ready?: boolean; error?: { code?: string } }) | undefined;
    if (result.isError || data?.error) {
      if (name === 'refresh' && !image.src) showEmpty(previousEmptyState);
      notify(FAILED[data?.error?.code ?? ''] ?? '操作未完成，请重试', true);
    } else {
      // A refresh answers with the new stream identity; take the next frame at once.
      if (name === 'refresh') consume(data);
      notify(name === 'refresh' && data?.service_ready === false ? '未连接到手机，请确认连接后重试' : DONE[name], name === 'refresh' && data?.service_ready === false);
      if (timer) clearTimeout(timer);
      timer = undefined;
      schedule(0);
    }
  } catch {
    if (name === 'refresh' && !image.src) showEmpty(previousEmptyState);
    notify('操作未完成，请重试', true);
  } finally {
    acting = false;
    delete tools[name].dataset.busy;
    syncTools();
  }
}
// Pointer coordinates are relative to the contained image, never the bezel or panel.
let decodedAt = -Infinity;
let freshUntil = -Infinity;
let failedFrame = false;
type PointerStart = { id: number; point: Point; client: Point; at: number; generation: number; stream?: string; size: Size; moved: boolean };
let pointer: PointerStart | undefined;
let viewportRetryAt = -Infinity;
async function recoverViewport() {
  if (!visible() || acting || root.dataset.live !== 'live' || root.dataset.inputBusy === 'true'
      || !validSize(dimensions) || !validSize(viewport)
      || Math.abs(dimensions.width / dimensions.height - viewport.width / viewport.height) < .01
      || performance.now() < viewportRetryAt) return;
  // A rotated MJPEG frame can arrive without any phone tool updating the cached viewport.
  // Refresh only on mismatch; failures are throttled and never replay a gesture.
  viewportRetryAt = performance.now() + 2000;
  const generation = frameGeneration, stream = streamId;
  try {
    const result = await app.callServerTool({
      name: 'pua_screen_action', arguments: { action: 'viewport' },
    }, { timeout: ACTION_TIMEOUT });
    const data = result.structuredContent as Partial<Preview> | undefined;
    if (visible() && generation === frameGeneration && stream === streamId
        && !result.isError && validSize(data?.viewport)) viewport = data.viewport;
  } catch { /* Keep input disabled until geometry can be verified by a later poll. */ }
}
function canPoint() {
  return visible() && !acting && root.dataset.live === 'live' && root.dataset.inputBusy !== 'true'
    && !failedFrame && !image.hidden && !!image.src && validSize(viewport) && validSize(dimensions)
    && Number.isFinite(decodedAt) && performance.now() < freshUntil
    && Math.abs(dimensions.width / dimensions.height - viewport.width / viewport.height) < .01;
}
function pointerPoint(event: MouseEvent): Point | undefined {
  if (!viewport || !dimensions) return;
  const rect = screen.getBoundingClientRect();
  const scale = Math.min(rect.width / dimensions.width, rect.height / dimensions.height);
  const width = dimensions.width * scale, height = dimensions.height * scale;
  const x = event.clientX - rect.left - (rect.width - width) / 2;
  const y = event.clientY - rect.top - (rect.height - height) / 2;
  if (width <= 0 || height <= 0 || x < 0 || y < 0 || x >= width || y >= height) return;
  return { x: x / width * viewport.width, y: y / height * viewport.height };
}
function cancelPointer() {
  const current = pointer;
  pointer = undefined;
  if (current && screen.hasPointerCapture(current.id)) screen.releasePointerCapture(current.id);
}
function pointerDown(event: PointerEvent) {
  if (!event.isPrimary) { cancelPointer(); return; }
  if (event.button !== 0 || !canPoint()) return;
  const point = pointerPoint(event);
  if (!point || !viewport) return;
  event.preventDefault();
  pointer = { id: event.pointerId, point, client: { x: event.clientX, y: event.clientY },
    at: performance.now(), generation: frameGeneration, stream: streamId, size: { ...viewport }, moved: false };
  screen.setPointerCapture(event.pointerId);
}
function pointerMove(event: PointerEvent) {
  if (pointer?.id === event.pointerId && Math.hypot(event.clientX - pointer.client.x, event.clientY - pointer.client.y) > 6) pointer.moved = true;
}
function pointerUp(event: PointerEvent) {
  const start = pointer;
  if (!start || start.id !== event.pointerId) return;
  pointerMove(event);
  const end = pointerPoint(event);
  cancelPointer();
  if (!end || !canPoint() || start.generation !== frameGeneration || start.stream !== streamId
      || start.size.width !== viewport?.width || start.size.height !== viewport?.height) return;
  // Long presses and looped drags are deliberately not converted into surprise taps.
  if (!start.moved && performance.now() - start.at > 700) return;
  if (start.moved && Math.hypot(end.x - start.point.x, end.y - start.point.y) < 3) return;
  void sendPointer({ action: start.moved ? 'drag' : 'tap', x: start.point.x, y: start.point.y,
    width: start.size.width, height: start.size.height,
    ...(start.moved ? { to_x: end.x, to_y: end.y, duration: Math.max(.05, Math.min(1, (performance.now() - start.at) / 1000)) } : {}) });
}
async function sendPointer(args: Record<string, string | number>) {
  acting = true;
  syncTools();
  try {
    const result = await app.callServerTool({ name: 'pua_screen_action', arguments: args }, { timeout: ACTION_TIMEOUT });
    const data = result.structuredContent as { error?: { code?: string; uncertain?: boolean } } | undefined;
    if (result.isError || data?.error) notify(data?.error?.uncertain ? '操作结果不确定，请查看手机后再操作' : FAILED[data?.error?.code ?? ''] ?? '操作未完成，请查看手机状态', true);
  } catch {
    notify('操作结果不确定，请查看手机后再操作', true);
  } finally {
    acting = false;
    freshUntil = -Infinity; // Require another fresh stream heartbeat before the next gesture.
    if (timer) clearTimeout(timer);
    timer = undefined;
    syncTools();
    schedule(0);
  }
}
// A trackpad emits many wheel events, including momentum after fingers lift.
// Send one bounded phone swipe per burst; never queue the remaining events.
let wheelAt = -Infinity;
let wheelX = 0, wheelY = 0;
let wheelConsumed = false;
let wheelGeneration = 0;
function wheel(event: WheelEvent) {
  const now = performance.now();
  if (now - wheelAt > 180) {
    wheelX = wheelY = 0;
    wheelConsumed = false;
    wheelGeneration = frameGeneration;
  }
  wheelAt = now;
  if (event.ctrlKey || event.metaKey) { wheelConsumed = true; return; } // pinch/zoom belongs to the host
  event.preventDefault();
  if (wheelConsumed) return;
  if (pointer || !canPoint() || wheelGeneration !== frameGeneration || !pointerPoint(event) || !viewport) {
    wheelConsumed = true;
    return;
  }
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? screen.getBoundingClientRect().height : 1;
  if (!Number.isFinite(event.deltaX) || !Number.isFinite(event.deltaY)) { wheelConsumed = true; return; }
  wheelX += event.deltaX * unit;
  wheelY += event.deltaY * unit;
  if (Math.max(Math.abs(wheelX), Math.abs(wheelY)) < 24) return;
  wheelConsumed = true;
  const horizontal = Math.abs(wheelX) > Math.abs(wheelY);
  const size = viewport;
  // Positive wheel delta scrolls content forward: the finger travels up/left.
  // Use the center so hovering near an edge cannot clip the phone gesture.
  const dx = horizontal ? -Math.sign(wheelX) * size.width * .65 : 0;
  const dy = horizontal ? 0 : -Math.sign(wheelY) * size.height * .45;
  void sendPointer({ action: 'drag', x: size.width / 2 - dx / 2, y: size.height / 2 - dy / 2,
    to_x: size.width / 2 + dx / 2, to_y: size.height / 2 + dy / 2,
    width: size.width, height: size.height, duration: .15 });
}
screen.addEventListener('wheel', wheel, { passive: false });
const pointerHandlers = { pointerdown: pointerDown, pointermove: pointerMove, pointerup: pointerUp,
  pointercancel: cancelPointer, lostpointercapture: cancelPointer };
for (const [name, handler] of Object.entries(pointerHandlers)) screen.addEventListener(name, handler as EventListener);
screen.title = '点击操作手机；拖动或触控板双指滑动来滚动、翻页';

const toolHandlers = (Object.keys(tools) as ToolName[]).map(name => {
  const handler = () => { void act(name); };
  tools[name].addEventListener('click', handler);
  return [name, handler] as const;
});

function visibilityChanged() {
  wheelConsumed = true;
  cancelPointer();
  root.dataset.pageVisible = String(visible());
  if (!visible()) {
    if (timer) clearTimeout(timer);
    timer = undefined;
    retainFrame();
  } else {
    if (timer) clearTimeout(timer);
    timer = undefined;
    schedule(0);
  }
}

const resizeObserver = new ResizeObserver(fitFrame);
resizeObserver.observe(stage);
image.onerror = () => {
  failedFrame = true;
  decodedAt = -Infinity;
  if (disposed || root.dataset.live === 'paused' || !image.src) return;
  if (lastGoodFrame && image.src !== lastGoodFrame.source) {
    image.src = lastGoodFrame.source;
    dimensions = lastGoodFrame.size;
    fitFrame();
  } else if (!lastGoodFrame) {
    clearFrame();
    setLive('offline');
    showEmpty('unavailable');
  }
  frameSeq = 0; // Request a replacement without blanking the last decoded frame.
  retainFrame();
};
function documentVisibilityChanged() {
  if (!document.hidden) suspended = false;
  visibilityChanged();
}
document.addEventListener('visibilitychange', documentVisibilityChanged);

function pageHide() {
  // WebViews may suspend a document without putting it in the browser cache.
  // A real navigation destroys this JS context; only host teardown is terminal.
  suspended = true;
  visibilityChanged();
}
function pageShow() { suspended = false; visibilityChanged(); }
function focusChanged() { if (!document.hidden) pageShow(); }

function dispose() {
  if (disposed) return;
  disposed = true;
  ready = false;
  root.dataset.pageVisible = 'false';
  if (timer) clearTimeout(timer);
  timer = undefined;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = undefined;
  toast.hidden = true;
  cancelPointer();
  screen.removeEventListener('wheel', wheel);
  for (const [name, handler] of Object.entries(pointerHandlers)) screen.removeEventListener(name, handler as EventListener);
  for (const [name, handler] of toolHandlers) tools[name].removeEventListener('click', handler);
  clearFrame();
  resizeObserver.disconnect();
  document.removeEventListener('visibilitychange', documentVisibilityChanged);
  window.removeEventListener('pagehide', pageHide);
  window.removeEventListener('pageshow', pageShow);
  window.removeEventListener('focus', focusChanged);
}

app.ontoolresult = (result) => {
  if (!result.isError) consume(result.structuredContent);
  schedule(0);
};
app.onhostcontextchanged = (context) => { applyTheme(context?.theme); };
app.onteardown = async () => { dispose(); return {}; };
app.onclose = () => {
  ready = false;
  retainFrame();
  schedule(1000);
};
window.addEventListener('pagehide', pageHide);
window.addEventListener('pageshow', pageShow);
window.addEventListener('focus', focusChanged);

function connect() {
  if (!connecting) connecting = initialize().finally(() => { connecting = undefined; });
  return connecting;
}

async function initialize() {
  await app.connect(undefined, { timeout: REQUEST_TIMEOUT });
  if (disposed) { await app.close(); return; }
  ready = true;
  const context = app.getHostContext();
  applyTheme(context?.theme);
  if (!requestedFullscreen && context?.displayMode !== 'fullscreen'
      && context?.availableDisplayModes?.includes('fullscreen')) {
    requestedFullscreen = true;
    try { await app.requestDisplayMode({ mode: 'fullscreen' }, { timeout: REQUEST_TIMEOUT }); } catch { /* Keep the preview in the host's supported view. */ }
  }
}

root.dataset.pageVisible = String(visible());
device.hidden = screen.hidden = false;
image.hidden = true;
emptyState.hidden = false;
showEmpty('connecting');
fitFrame();
syncTools();
try { await connect(); } catch { retainFrame(); setLive('offline'); }
schedule(ready ? 0 : 1000);
