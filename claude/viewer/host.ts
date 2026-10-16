// Claude Code 内置浏览器面板中的 MCP Apps 宿主：原样加载 Codex 使用的手机屏幕 widget，
// 把 widget 的工具调用转发给本机查看服务（screen_viewer.py），由同一个 Runtime 处理。
import { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';

const HOST_VERSION = '0.1.0';
// widget 仅调用这三个工具；查看服务同样只放行它们。
const WIDGET_TOOLS = new Set(['pua_screen', 'pua_screen_frame', 'pua_screen_action']);
const token = new URLSearchParams(location.search).get('token') ?? '';
const widget = document.getElementById('widget') as HTMLIFrameElement;
const darkScheme = matchMedia('(prefers-color-scheme: dark)');

function failed() {
  document.body.dataset.failed = 'true';
}

async function request(path: string, body?: unknown): Promise<Response> {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Viewer-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    credentials: 'omit',
  });
  if (!response.ok) throw new Error(`viewer ${path} ${response.status}`);
  return response;
}

async function callTool(name: string, args: Record<string, unknown> | undefined): Promise<CallToolResult> {
  if (!WIDGET_TOOLS.has(name)) return { content: [], isError: true };
  const response = await request('/tool', { name, arguments: args ?? {} });
  return await response.json() as CallToolResult;
}

function hostContext() {
  return {
    theme: darkScheme.matches ? 'dark' as const : 'light' as const,
    displayMode: 'fullscreen' as const,
    availableDisplayModes: ['fullscreen' as const],
    containerDimensions: { width: innerWidth, height: innerHeight },
    platform: 'desktop' as const,
    locale: navigator.language,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    userAgent: navigator.userAgent,
    deviceCapabilities: { touch: false, hover: true },
  };
}

async function main() {
  let html: string;
  try {
    html = await (await request('/widget')).text();
  } catch {
    failed();
    return;
  }
  const bridge = new AppBridge(
    null,
    { name: 'iPhone Use Viewer', version: HOST_VERSION },
    { serverTools: {}, logging: {} },
    { hostContext: hostContext() },
  );
  bridge.oncalltool = async (params) => callTool(params.name, params.arguments);
  // 面板本身就是全屏视图；widget 请求全屏时直接确认。
  bridge.onrequestdisplaymode = async () => ({ mode: 'fullscreen' });
  bridge.oninitialized = async () => {
    // 与 Codex 打开面板时一致：先给出调用参数，再推送一次 pua_screen 结果作为初始状态。
    await bridge.sendToolInput({ arguments: { action: 'open' } });
    try {
      await bridge.sendToolResult(await callTool('pua_screen', { action: 'open' }));
    } catch {
      // widget 自身会继续拉帧并显示未连接状态。
    }
  };
  const updateContext = () => bridge.setHostContext(hostContext());
  darkScheme.addEventListener('change', updateContext);
  addEventListener('resize', updateContext);
  // 先建立监听再加载 widget，避免错过其初始化请求。
  await bridge.connect(new PostMessageTransport(widget.contentWindow!, widget.contentWindow!));
  widget.srcdoc = html;
}

void main();
