// Opens one device port on the configured iPhone over the path the transport
// allows. USB uses usbmux's cable record. Wi-Fi uses the CoreDevice tunnel
// that Xcode keeps to the phone while the runner's test session is active: an
// encrypted, pairing-authenticated link whose phone-side address devicectl
// reports. usbmux "Network" records are never used: they are plain TCP on the
// LAN and disappear once the phone locks.
import {execFile} from 'node:child_process';
import net from 'node:net';
import {Usbmux, getDefaultSocket} from 'appium-ios-device/build/lib/usbmux/index.js';

export const TRANSPORTS = ['auto', 'usb', 'network'];
const CONNECT_TIMEOUT_MS = 5000;

export function parseTransport(value) {
  if (!TRANSPORTS.includes(value)) throw new Error(`Transport must be one of: ${TRANSPORTS.join(', ')}.`);
  return value;
}

// The cable tunnel, or null when usbmux lists no cable for this phone.
async function openUsb(udid, port) {
  const mux = new Usbmux(await getDefaultSocket());
  let connected = false;
  try {
    const record = (await mux.listDevices()).find(item => item?.Properties?.SerialNumber === udid
      && item.Properties.ConnectionType === 'USB');
    if (!record) return null;
    try {
      // Success turns this usbmux control socket into the device tunnel.
      const socket = await mux.connect(record.Properties.DeviceID, port);
      connected = true;
      return socket;
    } catch (error) {
      throw new Error(`USB connection to device port ${port} failed: ${error?.message ?? error}`);
    }
  } finally {
    if (!connected) mux.close();
  }
}

function listCoreDevices() {
  return new Promise((resolve, reject) => {
    execFile('xcrun', ['devicectl', 'list', 'devices', '--quiet', '--timeout', '5', '--json-output', '-'],
      {timeout: 10000, maxBuffer: 4 * 1024 * 1024}, (error, stdout) => {
        if (error) { reject(new Error(`devicectl failed: ${error.message}`)); return; }
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error('devicectl returned invalid JSON')); }
      });
  });
}

// The tunnel exists only while a CoreDevice session (the runner's xcodebuild)
// is active, so it is looked up again for every connection (~70 ms).
export async function findTunnel(udid) {
  let document;
  try { document = await listCoreDevices(); } catch (error) { return {reason: error.message}; }
  const devices = document?.result?.devices;
  const device = Array.isArray(devices) ? devices.find(item => item?.hardwareProperties?.udid === udid) : undefined;
  if (!device) return {reason: 'CoreDevice does not list it'};
  const connection = device.connectionProperties ?? {};
  if (connection.tunnelState !== 'connected') return {reason: `tunnelState=${connection.tunnelState ?? 'unknown'}`};
  // An IP literal only, so the address can never trigger a name lookup.
  const address = connection.tunnelIPAddress;
  if (typeof address !== 'string' || !net.isIPv6(address)) return {reason: 'no IPv6 tunnel address'};
  return {address, link: connection.transportType ?? 'unknown'};
}

function openTunnel(address, port) {
  return new Promise((resolve, reject) => {
    const socket = net.connect({host: address, port});
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error(`timed out after ${CONNECT_TIMEOUT_MS} ms`));
    }, CONNECT_TIMEOUT_MS);
    socket.once('error', error => { clearTimeout(timer); reject(error); });
    socket.once('connect', () => {
      clearTimeout(timer);
      // Hand it over paused, like a usbmux tunnel.
      socket.pause();
      resolve(socket);
    });
  });
}

export async function connectDevicePort(udid, port, transport) {
  parseTransport(transport);
  if (transport !== 'network') {
    // auto prefers the cable whenever usbmux lists it: unplugging moves new
    // connections to the tunnel, plugging back in returns them to USB.
    const socket = await openUsb(udid, port);
    if (socket) return {socket, path: 'USB'};
    if (transport === 'usb') throw new Error('No USB record for the configured iPhone (transport=usb).');
  }
  const tunnel = await findTunnel(udid);
  if (!tunnel.address) {
    throw new Error(transport === 'auto'
      ? `No USB record or CoreDevice tunnel for the configured iPhone (transport=auto): ${tunnel.reason}.`
      : `No CoreDevice tunnel to the configured iPhone (transport=network): ${tunnel.reason}.`);
  }
  try {
    return {socket: await openTunnel(tunnel.address, port), path: `CoreDevice tunnel (${tunnel.link})`};
  } catch (error) {
    const reason = error?.code === 'ECONNREFUSED' ? 'the port is not listening yet' : (error?.message ?? String(error));
    throw new Error(`CoreDevice tunnel connection to device port ${port} failed: ${reason}`);
  }
}
