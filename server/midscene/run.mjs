// Host decides from screenshots. This worker executes one explicit device action;
// it never calls aiAct/aiQuery/aiAssert or an external model.
import { readFileSync } from 'node:fs';
import { format } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { ActionReport } from './action-report.mjs';

console.log = (...args) => process.stderr.write(`${format(...args)}\n`);
console.info = console.log;
let device;
let report;
let screenshot;
let viewport;
let result;
let exitCode = 0;
let action;
try {
  const request = JSON.parse(readFileSync(0, 'utf8'));
  action = request.action;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(request.reportId ?? '')) throw new Error('Invalid report ID');
  const { IOSDevice } = await import('@midscene/ios');
  device = new IOSDevice({ wdaHost: request.host, wdaPort: request.port,
    sessionId: request.sessionId, autoDismissKeyboard: false });
  await device.connect();
  viewport = await device.size();
  const args = request.args ?? {};
  const point = (x, y) => {
    if (![x, y].every(Number.isFinite) || x < 0 || y < 0 || x >= viewport.width || y >= viewport.height)
      throw new Error('Point outside viewport');
    return { x, y };
  };
  // Capture before dispatch: a failed initial capture must not execute an action.
  screenshot = await device.screenshotBase64();
  report = new ActionReport(request.reportId, action, args, screenshot, viewport);
  await report.flush();
  // Do not return this pre-action frame as the resulting screen if dispatch fails.
  screenshot = undefined;
  report.startAction();
  if (action === 'tap') await device.tapPoint(point(args.x, args.y));
  else if (action === 'swipe') await device.swipePoint(point(args.x, args.y), point(args.end_x, args.end_y), 500);
  else if (action === 'input') await device.typeText(args.text, { autoDismissKeyboard: false });
  else if (action === 'home') await device.home();
  else if (action === 'launch') await device.launch(args.text);
  else if (!['screenshot', 'record'].includes(action)) throw new Error('Unsupported action');
  report.endAction();
  // Dispatch completion can precede iOS navigation animation; never retry it.
  if (['tap', 'swipe', 'input', 'home', 'launch'].includes(action)) await delay(600);
  screenshot = await device.screenshotBase64();
  viewport = await device.size();
  const verdictError = action === 'record' && args.passed === false
    ? 'Host reported the condition was not met' : undefined;
  await report.finish(screenshot, verdictError);
  result = { ok: !verdictError, action, action_complete: true, decision_source: 'chat_host' };
  if (verdictError) exitCode = 1;
} catch {
  result = { ok: false };
  exitCode = 1;
  if (report) {
    screenshot = await device.screenshotBase64().catch(() => undefined);
    await report.finish(screenshot, 'Device action or capture failed; inspect state before continuing').catch(() => {});
  }
} finally {
  if (device) {
    try { await device.destroy(); } catch { result.ok = false; exitCode = 1; }
  }
  if (report) {
    try { result.report = await report.finalize(); }
    catch { result.ok = false; exitCode = 1; }
  }
  if (screenshot && viewport) Object.assign(result, { screenshot, viewport });
}
process.stdout.write(JSON.stringify(result), () => process.exit(exitCode));
