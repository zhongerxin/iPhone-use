// Development transport only. Production builds still import the MCP Apps SDK.
export class App {
  ontoolresult?: (result: any) => void;
  onhostcontextchanged?: (context: any) => void;
  onteardown?: () => Promise<any>;
  onclose?: () => void;
  constructor(..._args: any[]) {}
  async connect(..._args: any[]) {}
  async close() {}
  getHostContext() { return { displayMode: 'fullscreen', theme: 'dark' }; }
  async requestDisplayMode(..._args: any[]) {}
  async callServerTool(params: any, options: { timeout: number }) {
    const start = performance.now();
    const response = await fetch('rpc', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params), signal: AbortSignal.timeout(options.timeout),
    });
    if (!response.ok) throw new Error(`Preview HTTP ${response.status}`);
    const text = await response.text();
    const result = JSON.parse(text);
    if (params.name === 'pua_screen_frame') {
      const root = document.getElementById('app')!;
      root.dataset.requestMs = String(Math.round(performance.now() - start));
      if (result.structuredContent?.frame) root.dataset.payloadBytes = String(text.length);
      root.dataset.devTransport = 'browser-to-stdio-mcp';
    }
    return result;
  }
}

const meter = document.createElement('output');
document.body.style.background = '#10141c';
meter.id = 'preview-diagnostics';
meter.style.cssText = 'position:fixed;bottom:6px;left:8px;right:8px;text-align:center;font:11px monospace;color:#aeb6c3;pointer-events:none';
document.body.append(meter);
let frames = 0;
let totalFrames = 0;
let started = performance.now();
document.getElementById('image')!.addEventListener('load', () => { frames++; totalFrames++; });
setInterval(() => {
  const now = performance.now();
  const root = document.getElementById('app')!;
  const fps = (frames * 1000 / (now - started)).toFixed(1);
  root.dataset.renderFps = fps;
  root.dataset.totalFrames = String(totalFrames);
  meter.textContent = `渲染 ${fps} fps · 请求 ${root.dataset.requestMs ?? '—'} ms · ${Math.round(Number(root.dataset.payloadBytes ?? 0) / 1024)} KB · 采集年龄 ${root.dataset.captureAgeMs ?? '—'} ms`;
  frames = 0; started = now;
}, 2000);
