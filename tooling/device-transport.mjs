// Share transport selection between WDA control and the independent MJPEG stream.
// Only connect to the configured device's paired CoreDevice tunnel; never scan LAN IPs.
import net from 'node:net';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const exec = promisify(execFile);

export function tunnelAddress(result, udid) {
  const connection = result?.connectionProperties;
  const address = connection?.tunnelIPAddress;
  if (result?.hardwareProperties?.udid !== udid || connection?.pairingState !== 'paired'
      || connection?.tunnelState !== 'connected' || !['wired', 'localNetwork'].includes(connection?.transportType)
      || net.isIP(address ?? '') !== 6 || !/^f[cd][0-9a-f:]+$/i.test(address)) {
    throw new Error('The configured iPhone has no connected, paired CoreDevice tunnel.');
  }
  return address;
}

export async function discover(udid, run = exec) {
  const directory = await mkdtemp(join(tmpdir(), 'iphone-use-tunnel-'));
  const output = join(directory, 'device.json');
  try {
    await run('xcrun', ['devicectl', 'device', 'info', 'details', '--device', udid,
      '--json-output', output], { timeout: 3500, maxBuffer: 1024 * 1024 });
    return tunnelAddress(JSON.parse(await readFile(output, 'utf8')).result, udid);
  } finally { await rm(directory, { recursive: true, force: true }); }
}

function connectTCP(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    const timeout = setTimeout(() => socket.destroy(new Error('Device tunnel connection timed out.')), 2000);
    socket.once('error', error => { clearTimeout(timeout); reject(error); });
    socket.once('connect', () => { clearTimeout(timeout); resolve(socket); });
  });
}

export function createConnector(usbConnect, lookup = discover, tcpConnect = connectTCP) {
  let cached;
  return async (udid, port) => {
    if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '') || !Number.isInteger(port) || port < 1024 || port > 65535)
      throw new Error('Invalid device or port.');
    // Do not let an unresponsive usbmux hold up wireless fallback. Dispose late sockets.
    let expired = false, timer;
    try {
      return await Promise.race([
        Promise.resolve().then(() => usbConnect(udid, port)).then(socket => {
          if (expired) { socket.destroy(); throw new Error('USB connection expired.'); }
          return socket;
        }),
        new Promise((_, reject) => { timer = setTimeout(() => {
          expired = true; reject(new Error('USB connection timed out.'));
        }, 5000); }),
      ]);
    } catch {
      // Fallback happens before forwarding bytes: failed phone commands are never replayed.
    } finally { clearTimeout(timer); }
    if (cached?.udid !== udid || cached.until <= Date.now()) {
      cached = { udid, host: await lookup(udid), until: Date.now() + 5000 };
    }
    const host = cached.host;
    try { return await tcpConnect(host, port); }
    catch (error) { cached = undefined; throw error; }
  };
}
