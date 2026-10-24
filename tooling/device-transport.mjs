// Connect only to the configured device: USB first, then its paired CoreDevice tunnel.
import net from 'node:net';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const execute = promisify(execFile);

export async function connectDevicePort(udid, port) {
  if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '') || !Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('Invalid device endpoint.');
  }
  let expired = false;
  let timer;
  try {
    const {default: iosDevice} = await import('appium-ios-device');
    return await Promise.race([
      iosDevice.utilities.connectPort(udid, port).then(socket => {
        if (expired) { socket.destroy(); throw new Error('USB connection expired.'); }
        return socket;
      }),
      new Promise((_, reject) => {
        timer = setTimeout(() => { expired = true; reject(new Error('USB unavailable.')); }, 1500);
      }),
    ]);
  } catch {
    expired = true;
  } finally {
    clearTimeout(timer);
  }

  const directory = await mkdtemp(join(tmpdir(), 'iphone-use-tunnel-'));
  let host;
  try {
    const output = join(directory, 'device.json');
    await execute('xcrun', ['devicectl', 'list', 'devices', '--json-output', output], {timeout: 6000, maxBuffer: 1024 * 1024});
    const details = JSON.parse(await readFile(output, 'utf8'));
    const device = details.result?.devices?.find(device => device.hardwareProperties?.udid === udid);
    const connection = device?.connectionProperties;
    host = connection?.tunnelIPAddress;
    if (details.info?.outcome !== 'success' || device?.hardwareProperties?.udid !== udid ||
        connection?.pairingState !== 'paired' || connection?.tunnelState !== 'connected' ||
        net.isIP(host) !== 6 || !/^f[cd]/i.test(host)) {
      throw new Error('No paired device tunnel available.');
    }
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
  return await new Promise((resolve, reject) => {
    const socket = net.connect({host, port});
    const timeout = setTimeout(() => socket.destroy(new Error('Device tunnel connection timed out.')), 2500);
    const failed = error => { clearTimeout(timeout); reject(error); };
    socket.once('error', failed);
    socket.once('connect', () => {
      clearTimeout(timeout);
      socket.removeListener('error', failed);
      resolve(socket);
    });
  });
}
