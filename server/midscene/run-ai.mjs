// Separate authorized AI path. The no-model device worker stays available.
import { readFileSync, existsSync } from 'node:fs';
import { format } from 'node:util';
import { join } from 'node:path';
import { ChatGPTAuth, AuthError } from './chatgpt-auth.mjs';
import { TapProgressGuard } from './ai-budget.mjs';
import { createChatGPTClient } from './chatgpt-client.mjs';
import { compactPlanningContext } from './compact-planning.mjs';
import { swipeInDevicePoints } from './swipe-coordinates.mjs';

process.umask(0o077);
console.log = (...args) => process.stderr.write(`${format(...args)}\n`);
console.info = console.log;
for (const key of Object.keys(process.env)) {
  if (key.startsWith('MIDSCENE_') || key.startsWith('OPENAI_')) delete process.env[key];
}
let device, agent, request, result = { ok: false }, screenshot, viewport;
let inferenceError, deadline, deadlineTimer, completionSummary;
const modelRequests = [];
const progress = new TapProgressGuard();
const parent = process.ppid;
// Exit on parent loss rather than continuing to mutate an unowned device.
const watchdog = setInterval(() => { if (process.ppid !== parent) process.exit(1); }, 250);
watchdog.unref();
try {
  request = JSON.parse(readFileSync(0, 'utf8'));
  if (!['act', 'assert', 'wait'].includes(request.action) || !/^[A-Za-z0-9_-]{1,64}$/.test(request.reportId) ||
      typeof request.args?.text !== 'string' || !request.args.text.trim() || request.args.text.length > 10000)
    throw new AuthError('invalid_ai_request');
  if (request.planning !== undefined && (!['balanced', 'compact'].includes(request.planning) || request.action !== 'act'))
    throw new AuthError('invalid_ai_request');
  const waitTimeout = request.args.timeout_ms ?? 15000;
  if ((request.action !== 'wait' && request.args.timeout_ms !== undefined) ||
      !Number.isInteger(waitTimeout) || waitTimeout < 1000 || waitTimeout > 60000)
    throw new AuthError('invalid_ai_request');
  const compact = request.action === 'act' && request.planning !== 'balanced';
  const controller = new AbortController();
  deadline = controller.signal;
  deadlineTimer = setTimeout(() => controller.abort(new AuthError(request.action === 'wait' ? 'wait_timeout' : 'midscene_budget_exhausted')), request.action === 'act' ? 300000 : request.action === 'wait' ? waitTimeout : 150000);
  deadlineTimer.unref();
  const auth = new ChatGPTAuth(join(process.cwd(), 'chatgpt'));
  const models = await auth.models();
  // Preserve the service's preferred ordering, limiting to the GPT protocol.
  const model = models.find(item => /^gpt-/.test(item.slug))?.slug;
  if (!model) throw new AuthError('no_compatible_model');
  result.model = model;
  const { IOSAgent, IOSDevice } = await import('@midscene/ios');
  device = new IOSDevice({ wdaHost: request.host, wdaPort: request.port,
    sessionId: request.sessionId, autoDismissKeyboard: false });
  async function guard() {
    if (!(await auth.status()).authorized) throw new AuthError('chatgpt_sign_in_required');
    const state = join(process.cwd(), 'screen-state.json');
    if (existsSync(state) && JSON.parse(readFileSync(state, 'utf8')).paused)
      throw new AuthError('preview_paused');
    const response = await fetch(`http://${request.host}:${request.port}/wda/locked`, { signal: AbortSignal.timeout(5000) });
    if ((await response.json()).value !== false) throw new AuthError('phone_locked');
  }
  const capture = device.screenshotBase64.bind(device);
  device.screenshotBase64 = async () => { await guard(); const frame = await capture(); progress.observe(frame); return frame; };
  const actions = device.actionSpace.bind(device);
  const allowed = new Set(['Tap', 'Swipe', 'Scroll', 'Input', 'IOSHomeButton', 'Launch']);
  device.actionSpace = () => actions().filter(action => allowed.has(action.name)).map(action => ({
    ...action,
    description: action.name === 'Input' ? 'Single-line input: replace overwrites the target field, clear empties it (value must be empty), typeOnly appends. Choose the mode matching the requested edit. Never enter credentials or submit.' : action.description,
    call: async (param, context) => {
      deadline.throwIfAborted();
      await guard();
      deadline.throwIfAborted();
      try { progress.beforeAction(action.name, param); }
      catch (error) { controller.abort(error); throw error; }
      if (action.name === 'Input' && (!['replace', 'clear', 'typeOnly'].includes(param.mode ?? 'replace') || (param.mode === 'clear' && String(param.value) !== '') || /[\r\n\t]/.test(String(param.value)) || String(param.value).length > 10000))
        throw new AuthError('invalid_single_line_input');
      if (action.name === 'Launch' && !/^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$/.test(param.uri))
        throw new AuthError('launch_requires_bundle_id');
      if (action.name === 'Swipe' && param.repeat !== undefined && param.repeat !== 1)
        throw new AuthError('unbounded_action');
      return action.call(action.name === 'Swipe' ? swipeInDevicePoints(param, context) : param, context);
    },
  }));
  await device.connect();
  const screen = await device.getScreenSize();
  viewport = { width: screen.width, height: screen.height };
  // Fast planning uses the same image long-edge budget as single-step observations.
  // Let the SDK resize and map coordinates together; assertions retain full resolution.
  const screenshotShrinkFactor = compact
    ? Math.max(1, Math.max(screen.width, screen.height) * screen.scale / 1568) : 1;
  agent = new IOSAgent(device, { generateReport: true, autoPrintReportMsg: false,
    reportFileName: `iphone-use-${request.reportId}`,
    reportAttributes: { 'data-group-id': `iphone-use-${request.reportId}` },
    cache: false, replanningCycleLimit: 24, waitAfterAction: 600, screenshotShrinkFactor,
    aiActContext: (compact ? compactPlanningContext : '') + 'Only perform the requested task. If authentication, password, PIN, OTP, or biometric confirmation is required, stop and report failure for user takeover. Never invent credentials. Do not repeat a tap on an unchanged screen; move obscured targets into view. Input supports replace, clear (empty value), and typeOnly (append). Replace or clear only the requested field; use single-line text and never implicitly submit. Before finishing, observe the requested final state. In your completion message, state the concrete facts observed and any conditions that remain unverified; do not claim success merely because an action was dispatched.',
    modelConfig: { MIDSCENE_MODEL_NAME: model, MIDSCENE_MODEL_FAMILY: /^gpt-6/.test(model) ? 'gpt-6' : 'gpt-5',
      MIDSCENE_MODEL_API_KEY: 'oauth-managed-by-iphone-use', MIDSCENE_MODEL_BASE_URL: 'http://127.0.0.1:1',
      MIDSCENE_MODEL_TIMEOUT: 60000, MIDSCENE_MODEL_RETRY_COUNT: 0 },
    createOpenAIClient: createChatGPTClient(auth, { signal: deadline, onMetrics: metrics => modelRequests.push(metrics), onError: error => { inferenceError = error instanceof AuthError ? error.code : 'chatgpt_inference_failed'; } }),
  });
  if (request.action === 'act') completionSummary = await agent.aiAct(request.args.text, { abortSignal: deadline, effort: compact ? 'fast' : 'balance' });
  else if (request.action === 'wait') await agent.aiWaitFor(request.args.text, { timeoutMs: waitTimeout, checkIntervalMs: 1000 });
  else await agent.aiAssert(request.args.text);
  deadline.throwIfAborted();
  result = { ...result, ok: true, action_complete: true, decision_source: 'chatgpt_oauth' };
} catch (error) {
  let authError = error;
  for (let depth = 0; depth < 8 && authError && !(authError instanceof AuthError); depth++) authError = authError.cause;
  if (deadline?.aborted) result.error = deadline.reason?.code ?? 'midscene_budget_exhausted';
  else if (inferenceError) result.error = inferenceError;
  else if (authError instanceof AuthError) result.error = authError.code;
  else if (error.message?.startsWith('Assertion failed:') && !error.cause) result.error = 'assertion_failed';
  else if (error.message?.includes('waitFor timeout:')) result.error = 'wait_timeout';
  else if (/Replanned \d+ times, exceeding the limit/.test(error.message ?? '')) result.error = 'midscene_cycle_limit';
  else result.error = 'midscene_ai_failed';
} finally {
  clearTimeout(deadlineTimer);
  if (device) {
    screenshot = await device.screenshotBase64().catch(() => undefined);
    viewport ??= await device.size().catch(() => undefined);
  }
  if (agent) {
    try { await agent.destroy(); result.report = agent.reportFile; }
    catch { result.ok = false; result.error = 'report_finalize_failed'; }
  } else if (device) await device.destroy().catch(() => {});
  if (screenshot && viewport) Object.assign(result, { screenshot, viewport });
  if (result.ok && request?.action === 'act') {
    result.completion = {
      source: 'midscene_aiAct',
      summary: typeof completionSummary === 'string' ? completionSummary : null,
      independent_assertion: false,
    };
  }
  clearInterval(watchdog);
  if (process.env.IPHONE_USE_MODEL_METRICS === '1') result.model_requests = modelRequests;
}
process.stdout.write(JSON.stringify(result), () => process.exit(result.ok ? 0 : 1));
