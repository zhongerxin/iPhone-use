// Public-client OAuth for ChatGPT plan usage. No Codex credentials or API keys.
import { createHash, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdir, readFile, lstat, open, rename, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { createRemoteJWKSet, jwtVerify } from 'jose';
import lockfile from 'proper-lockfile';

const ISSUER = 'https://auth.openai.com';
const RESOURCE = 'https://api.openai.com/v1';
const TOKEN_URL = `${ISSUER}/api/accounts/oauth/token`;
const PLAN_SCOPE = 'chatgpt.tokens.use.direct';
const jwks = createRemoteJWKSet(new URL(`${ISSUER}/.well-known/jwks.json`));
const random = () => randomBytes(32).toString('base64url');
const same = (a, b) => typeof a === 'string' && typeof b === 'string' &&
  Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));

export class AuthError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export async function verifyIdentity(token, clientId, nonce, subject, keys = jwks) {
  const { payload } = await jwtVerify(token, keys, {
    issuer: ISSUER, audience: clientId, requiredClaims: ['sub', 'exp', 'iat'],
    algorithms: ['RS256', 'ES256'], clockTolerance: 5,
  });
  if (typeof payload.sub !== 'string' || !payload.sub || (nonce !== undefined && !same(payload.nonce, nonce)) ||
      (subject && payload.sub !== subject)) throw new AuthError('identity_mismatch');
  return payload;
}

export class ChatGPTAuth {
  constructor(root, { fetchImpl = fetch, verify = verifyIdentity } = {}) {
    this.root = root;
    this.fetch = fetchImpl;
    this.verify = verify;
  }
  async init() {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const stat = await lstat(this.root);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077))
      throw new AuthError('unsafe_credential_directory');
  }
  async read(name) {
    try {
      const file = await open(join(this.root, name), 'r');
      try {
        const stat = await file.stat();
        const link = await lstat(join(this.root, name));
        if (!stat.isFile() || link.isSymbolicLink() || (stat.mode & 0o077))
          throw new AuthError('unsafe_credential_file');
        return JSON.parse(await file.readFile('utf8'));
      } finally { await file.close(); }
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  async write(name, value) {
    const path = join(this.root, name);
    const temp = `${path}.${random()}.tmp`;
    const file = await open(temp, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(value)); await file.sync(); }
    finally { await file.close(); }
    try { await rename(temp, path); } finally { await unlink(temp).catch(() => {}); }
  }
  async locked(fn) {
    await this.init();
    let release;
    try { release = await lockfile.lock(this.root, { stale: 120000, retries: 0 }); }
    catch { throw new AuthError('chatgpt_auth_busy'); }
    try { return await fn(); } finally { await release(); }
  }
  async json(url, options = {}) {
    let response;
    try { response = await this.fetch(url, { ...options, redirect: 'error', signal: AbortSignal.timeout(20000) }); }
    catch { throw new AuthError('chatgpt_network_error'); }
    if (!response.ok) {
      // Never expose OAuth response bodies: they may contain credentials.
      const body = await response.json().catch(() => ({}));
      if (body.error === 'invalid_grant') throw new AuthError('chatgpt_sign_in_required');
      throw new AuthError(`chatgpt_http_${response.status}`);
    }
    return response.json();
  }
  async exchange(params) {
    return this.json(TOKEN_URL, { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ ...params, resource: RESOURCE }).toString() });
  }
  validateTokens(tokens, previous) {
    const scopes = typeof tokens.scope === 'string' ? tokens.scope.split(' ') : previous?.scopes;
    if (!scopes?.includes(PLAN_SCOPE) || !scopes.includes('resource.invoke'))
      throw new AuthError('chatgpt_plan_not_authorized');
    if (typeof tokens.access_token !== 'string' || !tokens.access_token ||
        typeof tokens.refresh_token !== 'string' || !tokens.refresh_token ||
        tokens.token_type?.toLowerCase() !== 'bearer' ||
        !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0)
      throw new AuthError('invalid_token_response');
    return { access_token: tokens.access_token, refresh_token: tokens.refresh_token,
      scopes, expires_at: Date.now() + tokens.expires_in * 1000 };
  }
  async status() {
    await this.init();
    const record = await this.read('account.json');
    const login = await this.read('login.json');
    let alive = true;
    if (login?.status === 'pending') {
      try { process.kill(login.pid, 0); } catch { alive = false; }
    }
    return { authorized: Boolean(record?.access_token && record.scopes?.includes(PLAN_SCOPE)),
      account: record?.email ?? null, expires_at: record?.expires_at ?? null,
      login: login?.status === 'pending' && (login.expires_at < Date.now() || !alive)
        ? { status: 'expired' } : login };
  }
  async accessToken() {
    return this.locked(async () => {
      const record = await this.read('account.json');
      if (!record?.access_token || !record.scopes?.includes(PLAN_SCOPE))
        throw new AuthError('chatgpt_sign_in_required');
      if (record.expires_at > Date.now() + 60000) return record.access_token;
      const tokens = await this.exchange({ grant_type: 'refresh_token',
        client_id: record.client_id, refresh_token: record.refresh_token });
      const refreshed = this.validateTokens(tokens, record);
      if (tokens.id_token) {
        await this.verify(tokens.id_token, record.client_id, undefined, record.subject);
        refreshed.id_token = tokens.id_token;
      }
      await this.write('account.json', { ...record, ...refreshed });
      return refreshed.access_token;
    });
  }
  async models() {
    const token = await this.accessToken();
    const result = await this.json(`${RESOURCE}/models`, { headers: { Authorization: `Bearer ${token}` } });
    const models = (result.models ?? []).filter(m => m.visibility === 'list' &&
      typeof m.slug === 'string').map(m => ({ slug: m.slug, display_name: m.display_name ?? m.slug }));
    if (!models.length) throw new AuthError('no_available_models');
    return models;
  }
  async logout() {
    return this.locked(async () => {
      const record = await this.read('account.json');
      let revoked = !record?.refresh_token;
      // Disable local use immediately, even while remote revocation is pending.
      if (record) await this.write('account.json', { client_id: record.client_id,
        subject: record.subject, email: record.email, issuer: record.issuer });
      if (record?.refresh_token) {
        try {
          const discovery = await this.json(`${ISSUER}/.well-known/openid-configuration`);
          const url = new URL(discovery.revocation_endpoint);
          if (url.origin !== ISSUER) throw new AuthError('invalid_revocation_endpoint');
          const response = await this.fetch(url, { method: 'POST', redirect: 'error',
            signal: AbortSignal.timeout(20000), headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ token: record.refresh_token, token_type_hint: 'refresh_token', client_id: record.client_id }) });
          revoked = response.status === 200;
        } catch { /* Always clear local tokens, report unconfirmed revocation. */ }
      }
      await this.write('login.json', { status: 'signed_out' });
      return { authorized: false, remote_revocation_confirmed: revoked };
    });
  }
  async cancel() {
    const status = await this.status();
    if (status.login?.status !== 'pending') return { cancelled: false };
    const url = new URL(status.login.url);
    if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:' || !/^\/start\/[A-Za-z0-9_-]+$/.test(url.pathname))
      throw new AuthError('invalid_login_url');
    url.pathname = url.pathname.replace('/start/', '/cancel/');
    const response = await this.fetch(url, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new AuthError('authorization_in_progress');
    return { cancelled: true };
  }
  async login(onReady) {
    return this.locked(async () => {
      let host = await this.read('host.json');
      if (!host) { host = { id: `urn:uuid:${randomUUID()}` }; await this.write('host.json', host); }
      const previous = await this.read('account.json');
      const state = random(), nonce = random(), verifier = random(), start = random(), cookie = random();
      const expires = Date.now() + 600000;
      let used = false, timer, finish;
      const done = new Promise(resolve => { finish = resolve; });
      const server = createServer(async (req, res) => {
        res.setHeader('Cache-Control', 'no-store');
        res.setHeader('Referrer-Policy', 'no-referrer');
        res.setHeader('Content-Type', 'text/plain; charset=utf-8');
        try {
          if (req.headers.host !== `127.0.0.1:${server.address().port}`) {
            res.writeHead(400); res.end('Invalid request'); return;
          }
          const url = new URL(req.url, redirectUri);
          if (req.method === 'POST' && url.pathname === `/cancel/${start}` && !used) {
            used = true;
            await this.write('login.json', { status: 'cancelled' });
            res.end('Sign-in cancelled'); finish(); return;
          }
          if (req.method !== 'GET') { res.writeHead(400); res.end('Invalid request'); return; }
          if (url.pathname === `/start/${start}` && !used) {
            res.setHeader('Set-Cookie', `iphone_use_login=${cookie}; HttpOnly; SameSite=Lax; Path=/auth/callback; Max-Age=600`);
            res.writeHead(302, { Location: authorization.href }); res.end(); return;
          }
          if (url.pathname !== '/auth/callback' || used || Date.now() > expires ||
              url.searchParams.getAll('state').length !== 1 || !same(url.searchParams.get('state'), state) ||
              !req.headers.cookie?.split(';').some(c => c.trim() === `iphone_use_login=${cookie}`)) {
            res.writeHead(400); res.end('Invalid or expired sign-in callback'); return;
          }
          used = true;
          if (url.searchParams.has('error')) throw new AuthError('authorization_denied');
          for (const name of ['code', 'client_id']) if (url.searchParams.getAll(name).length > 1)
            throw new AuthError('invalid_callback');
          const clientId = url.searchParams.get('client_id') ?? previous?.client_id;
          if (!clientId?.startsWith('oaiapp_') || !url.searchParams.get('code') ||
              (previous && clientId !== previous.client_id)) throw new AuthError('client_mismatch');
          const tokens = await this.exchange({ grant_type: 'authorization_code', client_id: clientId,
            code: url.searchParams.get('code'), code_verifier: verifier, redirect_uri: redirectUri });
          const identity = await this.verify(tokens.id_token, clientId, nonce, previous?.subject);
          const credentials = this.validateTokens(tokens);
          if (Date.now() > expires) throw new AuthError('authorization_expired');
          await this.write('account.json', { ...credentials, client_id: clientId,
            issuer: identity.iss, subject: identity.sub, email: identity.email ?? null, id_token: tokens.id_token });
          await this.write('login.json', { status: 'authorized' });
          res.end('iPhone Use 已获得 ChatGPT 推理授权。可以关闭此页面并返回聊天。');
          finish();
        } catch (error) {
          if (used) {
            await this.write('login.json', { status: 'failed', error: error instanceof AuthError ? error.code : 'identity_validation_failed' }).catch(() => {});
            finish();
          }
          res.writeHead(400); res.end('授权未完成，请返回 iPhone Use 查看状态并重试。');
        }
      });
      await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
      const redirectUri = `http://127.0.0.1:${server.address().port}/auth/callback`;
      const authorization = new URL(`${ISSUER}/api/accounts/authorize`);
      authorization.search = new URLSearchParams({ client_id: previous?.client_id ?? 'dynamic_agent_client',
        ...(previous ? {} : { agent_name_hint: 'iPhone Use' }), ext_agent_host_id: host.id,
        ...(previous?.id_token ? { id_token_hint: previous.id_token } : {}),
        response_type: 'code', redirect_uri: redirectUri,
        scope: `openid profile email offline_access resource.invoke ${PLAN_SCOPE}`, resource: RESOURCE,
        state, nonce, code_challenge_method: 'S256', code_challenge: createHash('sha256').update(verifier).digest('base64url') });
      const info = { status: 'pending', url: `http://127.0.0.1:${server.address().port}/start/${start}`, expires_at: expires, pid: process.pid };
      try {
        await this.write('login.json', info);
        await onReady(info);
        timer = setTimeout(() => { used = true; finish(); }, 600000);
        await done;
        if ((await this.read('login.json'))?.status === 'pending') await this.write('login.json', { status: 'expired' });
      } finally { clearTimeout(timer); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
    });
  }
}
