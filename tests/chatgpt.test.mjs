import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from '../server/midscene/node_modules/jose/dist/webapi/index.js';
import { ChatGPTAuth, verifyIdentity } from '../server/midscene/chatgpt-auth.mjs';
import { createChatGPTClient } from '../server/midscene/chatgpt-client.mjs';
import { parseXMLPlanningResponse } from '../server/midscene/node_modules/@midscene/core/dist/es/ai-model/workflows/planning/standard-planning-parser.mjs';
import { ConversationHistory } from '../server/midscene/node_modules/@midscene/core/dist/es/ai-model/workflows/planning/conversation-history.mjs';
import { swipeInDevicePoints } from '../server/midscene/swipe-coordinates.mjs';

test('relative swipes use the current screenshot scale without changing reports or endpoints', () => {
  const param = { start: { center: [159, 547] }, direction: 'down', distance: 90, duration: 1500 };
  for (const ratio of [1, 2, 3, 1.5]) {
    const result = swipeInDevicePoints(param, { uiContext: { shrunkShotToLogicalRatio: ratio } });
    assert.equal(result.distance, 90 / ratio);
    assert.deepEqual(result.start, param.start);
    assert.equal(result.duration, 1500);
    assert.equal(param.distance, 90);
  }
  const endpoint = { start: { center: [159, 547] }, end: { center: [159, 577] } };
  assert.equal(swipeInDevicePoints(endpoint, {}), endpoint);
  for (const ratio of [undefined, 0, -1, NaN, Infinity])
    assert.throws(() => swipeInDevicePoints(param, { uiContext: { shrunkShotToLogicalRatio: ratio } }));
});

const scope = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct';
const tokens = { access_token: 'test-access', refresh_token: 'test-refresh', id_token: 'test-id',
  token_type: 'Bearer', expires_in: 3600, scope };
async function store(t, opts) {
  const root = await mkdtemp(join(tmpdir(), 'iphone-chatgpt-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const auth = new ChatGPTAuth(root, opts);
  await auth.init();
  return auth;
}

test('OIDC validates signature, issuer, audience, expiry, nonce and returning identity', async () => {
  const { privateKey, publicKey } = await generateKeyPair('RS256');
  const jwk = await exportJWK(publicKey); jwk.kid = 'test';
  const keys = createLocalJWKSet({ keys: [jwk] });
  const make = (claims = {}) => new SignJWT({ nonce: 'nonce', ...claims }).setProtectedHeader({ alg: 'RS256', kid: 'test' })
    .setIssuer('https://auth.openai.com').setAudience('oaiapp_test').setSubject('subject').setIssuedAt().setExpirationTime('1h').sign(privateKey);
  const jwt = await make();
  assert.equal((await verifyIdentity(jwt, 'oaiapp_test', 'nonce', 'subject', keys)).sub, 'subject');
  for (const args of [['other', 'nonce', 'subject'], ['oaiapp_test', 'wrong', 'subject'], ['oaiapp_test', 'nonce', 'other']])
    await assert.rejects(verifyIdentity(jwt, ...args, keys));
  await assert.rejects(verifyIdentity(jwt.slice(0, -12) + 'invalidxxxxx', 'oaiapp_test', 'nonce', null, keys));
  const expired = await new SignJWT({ nonce: 'nonce' }).setProtectedHeader({ alg: 'RS256', kid: 'test' })
    .setIssuer('https://auth.openai.com').setAudience('oaiapp_test').setSubject('subject').setIssuedAt(1).setExpirationTime(2).sign(privateKey);
  await assert.rejects(verifyIdentity(expired, 'oaiapp_test', 'nonce', null, keys));
});

test('loopback consent uses PKCE, rejects bad state, verifies identity and hides credentials', async t => {
  let exchange, verified;
  const auth = await store(t, {
    fetchImpl: async (url, opts) => { exchange = { url, params: new URLSearchParams(opts.body) }; return Response.json(tokens); },
    verify: async (...args) => { verified = args; return { sub: 'subject', iss: 'https://auth.openai.com', email: 'fixture@example.com' }; },
  });
  let ready;
  const started = new Promise(resolve => { ready = resolve; });
  const login = auth.login(ready);
  const info = await started;
  const redirect = await fetch(info.url, { redirect: 'manual' });
  const authorization = new URL(redirect.headers.get('location'));
  assert.equal(authorization.origin, 'https://auth.openai.com');
  assert.equal(authorization.searchParams.get('client_id'), 'dynamic_agent_client');
  assert.equal(authorization.searchParams.get('scope'), scope);
  assert.equal(authorization.searchParams.get('code_challenge_method'), 'S256');
  const callback = new URL(authorization.searchParams.get('redirect_uri'));
  const cookie = redirect.headers.get('set-cookie').split(';')[0];
  callback.search = new URLSearchParams({ code: 'code', state: 'wrong', client_id: 'oaiapp_test' });
  assert.equal((await fetch(callback, { headers: { Cookie: cookie } })).status, 400);
  assert.equal(exchange, undefined);
  callback.searchParams.set('state', authorization.searchParams.get('state'));
  assert.equal((await fetch(callback, { headers: { Cookie: cookie } })).status, 200);
  await login;
  assert.equal(exchange.params.get('client_id'), 'oaiapp_test');
  assert.equal(exchange.params.get('redirect_uri'), authorization.searchParams.get('redirect_uri'));
  assert.equal(verified[2], authorization.searchParams.get('nonce'));
  const { createHash } = await import('node:crypto');
  assert.equal(createHash('sha256').update(exchange.params.get('code_verifier')).digest('base64url'), authorization.searchParams.get('code_challenge'));
  assert.equal((await stat(join(auth.root, 'account.json'))).mode & 0o777, 0o600);
  const status = await auth.status();
  assert.equal(status.authorized, true);
  assert.ok(!JSON.stringify(status).includes('test-access'));
  assert.ok(!JSON.stringify(status).includes('test-id'));
});

test('denied consent retains an existing account and never exchanges code', async t => {
  const auth = await store(t, { fetchImpl: () => { throw new Error('must not exchange'); } });
  const existing = { client_id: 'oaiapp_old', subject: 'old', access_token: 'old', scopes: scope.split(' ') };
  await auth.write('account.json', existing);
  let ready; const started = new Promise(resolve => { ready = resolve; });
  const login = auth.login(ready); const info = await started;
  const redirect = await fetch(info.url, { redirect: 'manual' });
  const authorization = new URL(redirect.headers.get('location'));
  assert.equal(authorization.searchParams.get('client_id'), 'oaiapp_old');
  const callback = new URL(authorization.searchParams.get('redirect_uri'));
  callback.search = new URLSearchParams({ state: authorization.searchParams.get('state'), error: 'access_denied' });
  await fetch(callback, { headers: { Cookie: redirect.headers.get('set-cookie').split(';')[0] } });
  await login;
  assert.deepEqual(await auth.read('account.json'), existing);
  assert.equal((await auth.status()).login.error, 'authorization_denied');
});

test('pending login can be reopened or cancelled before a fresh attempt', async t => {
  const auth = await store(t);
  let ready; const started = new Promise(resolve => { ready = resolve; });
  const login = auth.login(ready); const info = await started;
  assert.equal((await fetch(info.url, { redirect: 'manual' })).status, 302);
  assert.equal((await fetch(info.url, { redirect: 'manual' })).status, 302);
  assert.deepEqual(await auth.cancel(), { cancelled: true });
  await login;
  assert.equal((await auth.status()).login.status, 'cancelled');
  assert.deepEqual(await auth.cancel(), { cancelled: false });
  assert.equal(await auth.read('account.json'), null);
});

test('refresh rotates credentials atomically and missing plan scope fails closed', async t => {
  let params;
  const auth = await store(t, { fetchImpl: async (_, opts) => { params = new URLSearchParams(opts.body); return Response.json({ ...tokens, id_token: undefined }); } });
  await auth.write('account.json', { client_id: 'oaiapp_test', subject: 'subject', access_token: 'old', refresh_token: 'old-refresh', scopes: scope.split(' '), expires_at: 1 });
  assert.equal(await auth.accessToken(), 'test-access');
  assert.equal(params.get('grant_type'), 'refresh_token');
  assert.equal(params.get('refresh_token'), 'old-refresh');
  assert.equal(params.has('scope'), false);
  assert.equal((await auth.read('account.json')).refresh_token, 'test-refresh');
  assert.throws(() => auth.validateTokens({ ...tokens, scope: 'openid' }), /chatgpt_plan_not_authorized/);
  // A second process cannot race a rotating token while a login/refresh owns it.
  await auth.locked(async () => { await assert.rejects(auth.accessToken(), /chatgpt_auth_busy/); });
});

test('failed remote revocation still clears local secrets and retains registration', async t => {
  const auth = await store(t, { fetchImpl: async () => { throw new Error('offline'); } });
  await auth.write('account.json', { ...tokens, client_id: 'oaiapp_test', subject: 'subject' });
  assert.deepEqual(await auth.logout(), { authorized: false, remote_revocation_confirmed: false });
  const raw = await readFile(join(auth.root, 'account.json'), 'utf8');
  assert.ok(!raw.includes('test-access') && !raw.includes('test-refresh') && !raw.includes('test-id'));
  assert.equal(JSON.parse(raw).client_id, 'oaiapp_test');
});

test('Responses adapter sends images, requires completion, drops unsupported fields and returns usage', async () => {
  let payload, clientOptions;
  class Client {
    constructor(opts) { clientOptions = opts; }
    responses = { create: async body => {
      payload = body;
      return (async function* () {
        yield { type: 'response.output_item.done', output_index: 0, item: { type: 'message',
          content: [{ type: 'output_text', text: '<data-json>{"StatementIsTruthy":true}</data-json>' }] } };
        yield { type: 'response.completed', response: { status: 'completed', model: 'gpt-fixture', output: [],
        usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 } } }; })();
    } };
  }
  const client = await createChatGPTClient({ accessToken: async () => 'oauth-fixture' }, { Client })();
  const result = await client.chat.completions.create({ model: 'gpt-fixture', temperature: 0, max_tokens: 1,
    messages: [{ role: 'system', content: 'instructions' }, { role: 'user', content: [
      { type: 'text', text: 'assert' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,fixture', detail: 'original' } }] }] });
  assert.equal(clientOptions.apiKey, 'oauth-fixture');
  assert.deepEqual(Object.keys(payload).sort(), ['input', 'model', 'store', 'stream']);
  assert.equal(payload.stream, true); assert.equal(payload.store, false);
  assert.equal(payload.input[0].role, 'developer');
  assert.equal(payload.input[1].content[1].type, 'input_image');
  assert.equal(payload.input[1].content[1].detail, 'original');
  assert.equal(result.usage.total_tokens, 15);
  assert.match(result.choices[0].message.content, /StatementIsTruthy/);
  class Incomplete { responses = { create: async () => (async function* () { yield { type: 'response.output_text.delta', delta: 'looks successful' }; })() }; }
  const broken = await createChatGPTClient({ accessToken: async () => 'x' }, { Client: Incomplete })();
  await assert.rejects(broken.chat.completions.create({ messages: [] }), /chatgpt_incomplete_stream/);
});

test('worker budget reaches the Responses request and cancels inference', async () => {
  const controller = new AbortController();
  let requestSignal;
  class Client {
    responses = { create: async (_body, options) => {
      requestSignal = options.signal;
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
      });
    } };
  }
  const factory = createChatGPTClient({ accessToken: async () => 'fixture' }, { Client, signal: controller.signal });
  const client = await factory();
  const pending = client.chat.completions.create({ model: 'fixture', messages: [] });
  await new Promise(resolve => setImmediate(resolve));
  controller.abort();
  await assert.rejects(pending);
  assert.equal(requestSignal.aborted, true);
});

test('model telemetry measures the stream, records failures, and excludes content', async () => {
  const events = [];
  class Client {
    responses = { create: async () => (async function* () {
      yield { type: 'response.created' };
      yield { type: 'response.output_text.delta', delta: 'private-output' };
      yield { type: 'response.completed', response: { status: 'completed', model: 'fixture',
        output: [{ type: 'message', content: [{ type: 'output_text', text: 'private-output' }] }],
        usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 } } };
    })() };
  }
  const client = await createChatGPTClient({ accessToken: async () => 'private-token' },
    { Client, onMetrics: m => events.push(m) })();
  await client.chat.completions.create({ model: 'fixture', messages: [{role:'user',content:'private-prompt'}] });
  assert.equal(events.length, 1);
  const m = events[0];
  assert.equal(m.ok, true);
  assert.ok(m.auth_ms <= m.request_start_ms && m.request_start_ms <= m.headers_ms);
  assert.ok(m.headers_ms <= m.first_event_ms && m.first_event_ms <= m.first_text_ms);
  assert.ok(m.first_text_ms <= m.last_text_ms && m.last_text_ms <= m.completed_ms && m.completed_ms <= m.total_ms);
  assert.equal(m.usage.input_tokens, 10);
  assert.doesNotMatch(JSON.stringify(events), /private-/);
  const failing = await createChatGPTClient({ accessToken: async () => { throw new Error('private-token'); } },
    { onMetrics: m => events.push(m) })();
  await assert.rejects(failing.chat.completions.create({model:'fixture',messages:[]}));
  assert.equal(events.length, 2);
  assert.equal(events[1].ok, false);
  assert.equal(events[1].headers_ms, undefined);
  assert.doesNotMatch(JSON.stringify(events), /private-/);
  const noisy = await createChatGPTClient({accessToken:async()=> 'token'},
    {Client,onMetrics:()=>{throw new Error('telemetry sink unavailable');}})();
  assert.equal((await noisy.chat.completions.create({model:'fixture',messages:[]})).choices[0].message.content,'private-output');
});

test('pinned SDK preserves explicit observations with fast planning and pruned screenshots', () => {
  const history = new ConversationHistory();
  history.append({ role: 'user', content: [{type:'image_url', image_url:{url:'old-screen'}}] });
  const { parsed } = parseXMLPlanningResponse(
    '<memory>{"observed":{"model":"iPhone 17 Pro"},"done":["read model"]}</memory>' +
    '<action-type>Tap</action-type><action-param-json>{"locate":{"bbox":[0,0,10,10]}}</action-param-json>',
    ['action-type','action-param-json'], {includeThought:false});
  assert.equal(parsed.thought, undefined);
  history.appendMemory(parsed.memory);
  history.append({role:'user',content:[{type:'image_url',image_url:{url:'new-screen'}}]});
  assert.match(JSON.stringify(history.snapshot(1)), /image ignored due to size optimization/);
  assert.match(history.memoriesToText(), /iPhone 17 Pro/);
  assert.match(history.memoriesToText(), /read model/);
  history.reset();
  assert.equal(history.memoriesToText(), '');
});

test('stuck taps stop, but changed frames, targets and other actions permit progress', async () => {
  const { TapProgressGuard } = await import('../server/midscene/ai-budget.mjs');
  const guard = new TapProgressGuard();
  const tap = { locate: { center: [10, 20] } };
  guard.observe('frame-1');
  guard.beforeAction('Tap', tap);
  guard.beforeAction('Tap', tap);
  assert.throws(() => guard.beforeAction('Tap', tap), { code: 'midscene_no_progress' });
  guard.observe('frame-2');
  guard.beforeAction('Tap', tap);
  guard.beforeAction('Tap', { locate: { center: [20, 30] } });
  guard.beforeAction('Input', { value: 'hello' });
  guard.beforeAction('Tap', tap);
});
