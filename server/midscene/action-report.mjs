// Record real host-directed device execution using Midscene's public report API.
// No model/planning tasks or independent AI assertions are fabricated.
import { randomUUID } from 'node:crypto';
import { ExecutionDump, ReportGenerator, ScreenshotItem, getVersion } from '@midscene/core';

const names = { tap: 'Tap', swipe: 'Swipe', input: 'Input', home: 'IOSHomeButton', launch: 'Launch' };

export class ActionReport {
  constructor(reportId, action, args, screenshot, viewport) {
    const filename = `iphone-use-${reportId}`;
    this.generator = ReportGenerator.create(filename, { generateReport: true, reuseExistingReport: true });
    this.attributes = { 'data-group-id': filename };
    const png = Buffer.from(screenshot.split(',').at(-1), 'base64');
    const shotSize = { width: png.readUInt32BE(16), height: png.readUInt32BE(20) };
    const locate = (x, y) => ({ center: [x * shotSize.width / viewport.width, y * shotSize.height / viewport.height] });
    const param = action === 'tap' ? { locate: locate(args.x, args.y) }
      : action === 'swipe' ? { start: locate(args.x, args.y), end: locate(args.end_x, args.end_y), duration: 500 }
        : action === 'input' ? { value: args.text, autoDismissKeyboard: false }
          : action === 'launch' ? { uri: args.text }
            : action === 'record' ? { content: args.text, passed: args.passed, decision_source: 'chat_host' } : {};
    const now = Date.now();
    const before = ScreenshotItem.create(screenshot, now);
    this.task = { taskId: randomUUID(), type: names[action] ? 'Action Space' : 'Log',
      subType: names[action] ?? (action === 'record' ? 'Host verification' : 'Screenshot'),
      param, status: 'running', timing: { start: now },
      uiContext: { screenshot: before, shotSize },
      recorder: [{ type: 'screenshot', ts: now, screenshot: before, description: 'Before' }] };
    this.execution = new ExecutionDump({ id: randomUUID(), logTime: now,
      name: this.task.subType, description: 'Decided by the chat host; executed by Midscene iOS. No model inference.', tasks: [this.task] });
  }

  async flush() {
    this.generator.onExecutionUpdate(this.execution, { groupName: 'iPhone Use',
      groupDescription: 'Host-driven device actions', sdkVersion: getVersion(), modelBriefs: [], deviceType: 'ios' }, this.attributes);
    await this.generator.flush();
  }

  startAction() { this.task.timing.callActionStart = Date.now(); }
  endAction() { this.task.timing.callActionEnd = Date.now(); }

  async finish(screenshot, error) {
    const now = Date.now();
    if (screenshot) this.task.recorder.push({ type: 'screenshot', ts: now,
      screenshot: ScreenshotItem.create(screenshot, now), description: 'After' });
    this.task.status = error ? 'failed' : 'finished';
    if (error) this.task.errorMessage = error;
    Object.assign(this.task.timing, { end: now, cost: now - this.task.timing.start });
    await this.flush();
  }

  async finalize() { return this.generator.finalize(); }
}
