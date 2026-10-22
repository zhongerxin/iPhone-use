// claude plugin test：READY / 屏幕工具返回后自动打开或复用右侧浏览器面板中的实时手机屏幕。
import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const READY = 'mcp__plugin_iphone-use_iphone_use__pua_ready'
const SCREEN = 'mcp__plugin_iphone-use_iphone_use__pua_screen'
const BROWSER = 'mcp__Claude_Browser__'
const VIEWER_URL = 'http://127.0.0.1:50123/?token=secret'
const VIEWER_ORIGIN = 'http://127.0.0.1:50123'

type World = { tabs?: { tabId: string; origin: string; isActive?: boolean }[]; hidden?: boolean; browser?: boolean; ready?: boolean }
type Recorded = { processes: string[][]; browserCalls: { tool: string; input: Record<string, unknown> }[]; traces: { path: string; record: Record<string, unknown> }[] }

function world(on: On, options: World = {}): Recorded {
  const recorded: Recorded = { processes: [], browserCalls: [], traces: [] }
  mock.env(on, { HOME: '/Users/tester' })
  on('fs.write', ($, e) => {
    recorded.traces.push({ path: e.path, record: JSON.parse(e.text) })
    return { value: undefined } as never
  })
  on('process.run', ($, e) => {
    recorded.processes.push([...e.argv])
    return { value: { exitCode: 0, stdout: JSON.stringify({ url: VIEWER_URL, pid: 1, reused: false }), stderr: '' } } as never
  })
  on('tool.call', { tool: READY }, () => ({ result: {}, text: JSON.stringify({ ready: options.ready ?? true, state: 'ready' }) }) as never)
  on('tool.call', { tool: SCREEN }, () => ({ result: {}, text: '{"paused":false}' }) as never)
  on('tool.call', ($, e, next) => {
    // 没有浏览器面板的宿主中这些工具不存在：交给引擎，调用会被拒绝。
    if (!e.tool.startsWith(BROWSER) || options.browser === false) return next(e)
    // tool_use_id 由引擎为每次调用生成，不属于钩子传入的参数。
    const { tool, tool_use_id: _callId, ...input } = e as { tool: string; tool_use_id?: string } & Record<string, unknown>
    recorded.browserCalls.push({ tool: tool.slice(BROWSER.length), input })
    const text = tool.endsWith('tabs_context')
      ? JSON.stringify({ browserOpen: (options.tabs ?? []).length > 0, tabs: options.tabs ?? [] }, null, 2)
        + (options.hidden ? '\nThe Browser pane is currently hidden.' : '')
      : 'ok'
    return { result: {}, text } as never
  })
  return recorded
}

test('READY 成功后启动查看服务并在新标签页打开画面', async ($, on) => {
  mock.store(on)
  const recorded = world(on)
  const result = await $.tool.call({ tool: READY } as never)
  expect(recorded.processes.length).toBe(1)
  expect(recorded.processes[0]?.slice(-2)).toEqual(['viewer/screen_viewer.py', 'start'].map((part, index) =>
    index === 0 ? expect.stringContaining(part) : part) as never)
  expect(recorded.browserCalls.map(call => call.tool)).toEqual(['tabs_context', 'preview_start'])
  expect(recorded.browserCalls[1]?.input).toEqual({ url: VIEWER_URL })
  expect(JSON.stringify(result)).toContain('不要再次启动查看服务')
  expect(recorded.traces.length).toBe(1)
  expect(recorded.traces[0]?.path).toBe('/Users/tester/.local/share/iphone-use/claude-hook.json')
  expect(recorded.traces[0]?.record).toEqual(expect.objectContaining({ trigger: 'pua_ready', opened: true }))
  expect(JSON.stringify(recorded.traces)).not.toContain('secret')
})

test('已有同一地址的标签页时只切换过去，不新开', async ($, on) => {
  mock.store(on)
  const recorded = world(on, { tabs: [{ tabId: 'other', origin: 'http://localhost:3000', isActive: true }, { tabId: 'viewer', origin: VIEWER_ORIGIN }] })
  await $.tool.call({ tool: READY } as never)
  expect(recorded.browserCalls.map(call => call.tool)).toEqual(['tabs_context', 'tabs_select'])
  expect(recorded.browserCalls[1]?.input).toEqual({ tabId: 'viewer' })
})

test('本插件打开过的旧端口标签页改为导航到新地址，用户自己的本机页面不动', async ($, on) => {
  mock.store(on, { 'viewer-origins': ['http://127.0.0.1:41000'] })
  const recorded = world(on, { tabs: [{ tabId: 'mine', origin: 'http://127.0.0.1:8080' }, { tabId: 'old', origin: 'http://127.0.0.1:41000' }] })
  await $.tool.call({ tool: READY } as never)
  expect(recorded.browserCalls.map(call => call.tool)).toEqual(['tabs_context', 'navigate'])
  expect(recorded.browserCalls[1]?.input).toEqual({ tabId: 'old', url: VIEWER_URL })
})

test('面板被收起时提示用户展开，而不是重复打开', async ($, on) => {
  mock.store(on)
  const recorded = world(on, { tabs: [{ tabId: 'viewer', origin: VIEWER_ORIGIN, isActive: true }], hidden: true })
  const result = await $.tool.call({ tool: READY } as never)
  expect(recorded.browserCalls.map(call => call.tool)).toEqual(['tabs_context'])
  expect(JSON.stringify(result)).toContain('展开右侧浏览器面板')
})

test('READY 未就绪、没有浏览器面板或暂停预览时什么也不做', async ($, on) => {
  mock.store(on)
  const notReady = world(on, { ready: false })
  await $.tool.call({ tool: READY } as never)
  expect(notReady.processes.length).toBe(0)
  await $.tool.call({ tool: SCREEN, action: 'pause' } as never)
  expect(notReady.processes.length).toBe(0)
  expect(notReady.browserCalls.length).toBe(0)
})

test('终端等没有浏览器面板的宿主不启动查看服务', async ($, on) => {
  mock.store(on)
  const recorded = world(on, { browser: false })
  const result = await $.tool.call({ tool: SCREEN } as never)
  expect(recorded.processes.length).toBe(0)
  expect(JSON.stringify(result)).not.toContain('iPhone Use：')
  expect(recorded.traces[0]?.record).toEqual(expect.objectContaining({ trigger: 'pua_screen', opened: false, steps: ['no browser pane tools'] }))
})
