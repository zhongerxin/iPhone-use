import { resolve } from 'node:path';
import { ChatGPTAuth, AuthError } from './chatgpt-auth.mjs';

process.umask(0o077);
const [action, root] = process.argv.slice(2);
const auth = new ChatGPTAuth(resolve(root));
const emit = value => process.stdout.write(`${JSON.stringify(value)}\n`);
try {
  if (action === 'login') await auth.login(info => emit({ ok: true, ...info }));
  else if (action === 'status') emit({ ok: true, ...await auth.status() });
  else if (action === 'logout') emit({ ok: true, ...await auth.logout() });
  else if (action === 'cancel') emit({ ok: true, ...await auth.cancel() });
  else if (action === 'models') emit({ ok: true, models: await auth.models() });
  else throw new AuthError('invalid_auth_action');
} catch (error) {
  emit({ ok: false, error: error instanceof AuthError ? error.code : 'chatgpt_auth_failed' });
  process.exitCode = 1;
}
