// Local browser transport for the production stdio MCP server.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Client } from '../../ui/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { StdioClientTransport } from '../../ui/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';

const [python, htmlPath, port = '0', token = randomBytes(24).toString('base64url')] = process.argv.slice(2);
if (!python || !htmlPath || !/^[\w-]+$/.test(token)) throw Error('Usage: node scripts/android-preview/server.mjs PYTHON HTML [PORT] [TOKEN]');
const root = fileURLToPath(new URL('../../', import.meta.url));
const client = new Client({ name: 'android-browser-preview', version: '1.0' });
await client.connect(new StdioClientTransport({ command: python, args: [root + 'server/android_use.py'], stderr: 'inherit' }));
await client.callTool({ name: 'pua_setup', arguments: { action: 'status' } });
const ready = await client.callTool({ name: 'pua_ready', arguments: { screenshot: false } });
if (!JSON.parse(ready.content[0].text).ready) { await client.close(); throw Error('Unlock and ready the selected Android first.'); }
const prefix = '/' + token + '/';
const server = createServer(async (req, res) => {
  const address = `127.0.0.1:${server.address().port}`;
  const reply = (status, body, type = 'application/json') => {
    res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    res.end(body);
  };
  if (req.headers.host !== address || (req.headers.origin && req.headers.origin !== 'http://' + address)) return reply(403, '{}');
  try {
    if (req.method === 'GET' && req.url === prefix) return reply(200, await readFile(htmlPath), 'text/html; charset=utf-8');
    if (req.method !== 'POST' || req.url !== prefix + 'rpc') return reply(404, '{}');
    const chunks = []; let length = 0;
    for await (const chunk of req) {
      length += chunk.length;
      if (length > 4096) return reply(413, '{}');
      chunks.push(chunk);
    }
    const params = JSON.parse(Buffer.concat(chunks).toString());
    if (!['pua_screen_frame', 'pua_screen_action'].includes(params.name)) return reply(400, '{}');
    const result = await client.callTool(params, undefined, { timeout: params.name === 'pua_screen_frame' ? 3000 : 12000 });
    reply(200, JSON.stringify(result));
  } catch (error) {
    reply(503, JSON.stringify({ error: String(error.message).slice(0, 160) }));
  }
});
server.listen(Number(port), '127.0.0.1', () => console.log(`http://127.0.0.1:${server.address().port}${prefix}`));
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  server.close(); server.closeAllConnections();
  await client.close();
}
process.on('SIGTERM', close);
process.on('SIGINT', close);
