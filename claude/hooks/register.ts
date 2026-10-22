// pua_ready 成功或 pua_screen(open) 返回后，自动启动本机查看服务，并在桌面端 Code 标签页右侧的
// 内置浏览器面板中打开实时手机屏幕；已有同一预览标签页时只切换过去。没有浏览器面板的宿主
// （如终端中的 Claude Code）不做任何事，由补充技能把地址交给用户。
import type { EngineInterface, Register } from 'claude-code'

const BROWSER = 'mcp__Claude_Browser__'
// 本插件打开过的预览地址（origin）；端口变化时只复用这些标签页，不改动用户自己的本机页面。
const ORIGINS_KEY = 'viewer-origins'
const MAX_ORIGINS = 8
// 最近一次自动打开的执行记录，写在运行目录中，便于排查；只记端口，不记令牌。
const TRACE_NAME = 'claude-hook.json'

type Engine = EngineInterface
type BrowserTab = { tabId: string; origin: string; isActive?: boolean }
type Outcome = { opened: boolean; note?: string }
type Trace = { trigger: string; steps: string[] }

let opening: Promise<Outcome> | undefined

function parseJson<T>(text: string | undefined): T | undefined {
  if (!text) return undefined
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start < 0 || end <= start) return undefined
  try {
    return JSON.parse(text.slice(start, end + 1)) as T
  } catch {
    return undefined
  }
}

async function stateDirectory($: Engine): Promise<string | undefined> {
  const configured = await $.env.get('IPHONE_USE_STATE_DIR')
  if (configured) return configured
  const home = await $.env.get('HOME')
  return home ? `${home}/.local/share/iphone-use` : undefined
}

async function writeTrace($: Engine, trace: Trace, extra: Record<string, unknown> = {}) {
  try {
    const directory = await stateDirectory($)
    if (!directory) return
    const record = { at: new Date().toISOString(), plugin: $.plugin.root, ...trace, ...extra }
    await $.fs.write(`${directory}/${TRACE_NAME}`, JSON.stringify(record, null, 2) + '\n')
  } catch {
    // 记录失败不影响打开画面。
  }
}

async function rememberedOrigins($: Engine): Promise<string[]> {
  const stored = await $.store.get(ORIGINS_KEY)
  return Array.isArray(stored) ? stored.filter((item): item is string => typeof item === 'string') : []
}

async function remember($: Engine, origin: string) {
  const origins = (await rememberedOrigins($)).filter(item => item !== origin)
  await $.store.set(ORIGINS_KEY, [origin, ...origins].slice(0, MAX_ORIGINS))
}

// 浏览器工具可能延迟加载而不在工具列表中，直接调用：不存在或出错即视为没有浏览器面板。
async function browser($: Engine, name: string, input: Record<string, unknown> = {}): Promise<string | undefined> {
  try {
    const result = await $.tool.call({ tool: `${BROWSER}${name}`, ...input } as never)
    return result.deny === undefined && result.isError !== true ? (result.text ?? '') : undefined
  } catch {
    return undefined
  }
}

async function openViewer($: Engine, trace: Trace): Promise<Outcome> {
  const context = await browser($, 'tabs_context')
  if (context === undefined) {
    trace.steps.push('no browser pane tools')
    return { opened: false }
  }
  const started = await $.process.run(['python3', `${$.plugin.root}/viewer/screen_viewer.py`, 'start'], { timeoutMs: 20_000 })
  const viewer = started.exitCode === 0 ? parseJson<{ url?: string }>(started.stdout) : undefined
  if (!viewer?.url) {
    trace.steps.push(`viewer start failed: exit ${started.exitCode} ${started.stderr.slice(-300)}`)
    return { opened: false, note: '手机屏幕查看服务未能启动，可稍后运行 viewer/screen_viewer.py start 重试；不影响手机任务。' }
  }
  const origin = new URL(viewer.url).origin
  trace.steps.push(`viewer ${origin}`)

  const state = parseJson<{ browserOpen?: boolean; tabs?: BrowserTab[] }>(context)
  const isHidden = /currently hidden/i.test(context)
  const tabs = state?.tabs ?? []
  const current = tabs.find(tab => tab.origin === origin)
  const previous = await rememberedOrigins($)
  const stale = tabs.find(tab => tab.origin !== origin && previous.includes(tab.origin))
  trace.steps.push(`tabs ${tabs.map(tab => tab.origin).join(',') || 'none'}${isHidden ? ' (hidden)' : ''}`)

  let action: string
  if (current) {
    action = current.isActive ? 'already active' : 'tabs_select'
    if (!current.isActive) await browser($, 'tabs_select', { tabId: current.tabId })
  } else if (stale) {
    action = 'navigate'
    await browser($, 'navigate', { tabId: stale.tabId, url: viewer.url })
  } else {
    action = 'preview_start'
    if (await browser($, 'preview_start', { url: viewer.url }) === undefined) {
      trace.steps.push('preview_start failed')
      return { opened: false, note: `未能打开浏览器面板，请让用户在右侧浏览器面板中打开 ${viewer.url} 查看实时手机屏幕。` }
    }
  }
  trace.steps.push(action)
  await remember($, origin)
  return {
    opened: true,
    note: isHidden && (current || stale)
      ? '实时手机屏幕已在右侧浏览器面板的标签页中，但面板当前处于隐藏状态：请提示用户展开右侧浏览器面板。不要再次打开或启动查看服务。'
      : '实时手机屏幕已在右侧浏览器面板中自动打开或复用。不要再次启动查看服务或打开浏览器标签页。',
  }
}

// READY 与屏幕工具可能先后返回；同一时刻只做一次打开。
function openOnce($: Engine, trigger: string): Promise<Outcome> {
  opening ??= (async () => {
    const trace: Trace = { trigger, steps: [] }
    try {
      const outcome = await openViewer($, trace)
      await writeTrace($, trace, { opened: outcome.opened })
      return outcome
    } catch (error) {
      await writeTrace($, trace, { opened: false, error: String(error).slice(0, 300) })
      return { opened: false }
    }
  })().finally(() => { opening = undefined })
  return opening
}

function withNote<T extends { context?: readonly string[] }>(result: T, outcome: Outcome): T {
  return outcome.note ? { ...result, context: [...(result.context ?? []), `iPhone Use：${outcome.note}`] } : result
}

// 打开画面只是附带效果：钩子出错时原样交还工具结果，不影响 READY 或屏幕工具本身。
export const register: Register = on => {
  on('tool.call', { tool: 'mcp__plugin_iphone-use_iphone_use__pua_ready' }, async ($, e, next) => {
    const result = await next(e)
    if (result.deny !== undefined || result.isError === true || !/"ready":true/.test(result.text ?? '')) return result
    return withNote(result, await openOnce($, 'pua_ready'))
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__plugin_iphone-use_iphone_use__pua_screen' }, async ($, e, next) => {
    const result = await next(e)
    const action = (e as { action?: unknown }).action
    if (result.deny !== undefined || result.isError === true || (action !== undefined && action !== 'open')) return result
    return withNote(result, await openOnce($, 'pua_screen'))
  }).catch(($, e, next) => next(e))
}
