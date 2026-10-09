// Explicit real-device, read-only check. Never print frame contents.
import { Client } from '../../ui/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { StdioClientTransport } from '../../ui/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';
import { fileURLToPath } from 'node:url';
const [python, serial, duration = '10'] = process.argv.slice(2);
const seconds = Number(duration);
if (!python || !serial || !Number.isFinite(seconds) || seconds < 1 || seconds > 60)
  throw Error('Usage: node scripts/android-preview/smoke.mjs PYTHON SELECTED_SERIAL [SECONDS:1..60]');
const client = new Client({ name: 'android-preview-smoke', version: '1.0' });
try {
  await client.connect(new StdioClientTransport({ command: python,
    args: [fileURLToPath(new URL('../../server/android_use.py', import.meta.url))], stderr: 'inherit' }));
  const status = await client.callTool({ name: 'pua_setup', arguments: { action: 'status' } });
  if (JSON.parse(status.content[0].text).selected_serial !== serial) throw Error('Selected device differs from requested device');
  const ready = await client.callTool({ name: 'pua_ready', arguments: { screenshot: false, recover: false } });
  if (!JSON.parse(ready.content[0].text).ready) throw Error('Selected device is not READY');
  let seq = 0, frames = 0, maxBytes = 0, unavailableAfterFrame = 0;
  const transports = {};
  const latencies = [];
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const start = performance.now();
    const reply = await client.callTool({ name: 'pua_screen_frame', arguments: { after_seq: seq } }, undefined, { timeout: 3000 });
    if (reply.isError) throw Error('Frame response failed');
    const preview = reply.structuredContent;
    transports[preview?.transport ?? 'unknown'] = (transports[preview?.transport ?? 'unknown'] ?? 0) + 1;
    if (frames && preview?.frame_available === false) unavailableAfterFrame++;
    latencies.push(performance.now() - start);
    const frame = reply.structuredContent?.frame;
    if (frame) { seq = frame.seq; frames++; maxBytes = Math.max(maxBytes, frame.data.length); }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  if (!frames) throw Error('No actual preview frame received');
  latencies.sort((a, b) => a - b);
  console.log(JSON.stringify({ seconds, calls: latencies.length, frames, unavailableAfterFrame, transports, maxBase64Bytes: maxBytes,
    p95RpcMs: Math.round(latencies[Math.floor(latencies.length * .95)]), maxRpcMs: Math.round(latencies.at(-1)) }));
  if (unavailableAfterFrame) throw Error('Preview became unavailable after receiving frames');
} finally { await client.close(); }
