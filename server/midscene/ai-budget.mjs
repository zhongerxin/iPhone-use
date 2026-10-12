import { createHash } from 'node:crypto';
import { AuthError } from './chatgpt-auth.mjs';

// An unchanged frame plus the same resolved tap is evidence of a stuck loop.
// This intentionally does not classify scrolling or typing as no progress.
export class TapProgressGuard {
  frame;
  previous;
  repeats = 0;
  observe(screenshot) {
    this.frame = createHash('sha256').update(screenshot).digest('hex');
  }
  beforeAction(name, param) {
    const center = param?.locate?.center;
    const key = name === 'Tap' && this.frame && center
      ? JSON.stringify([this.frame, center]) : undefined;
    this.repeats = key && key === this.previous ? this.repeats + 1 : 1;
    this.previous = key;
    if (key && this.repeats >= 3) throw new AuthError('midscene_no_progress');
  }
}
