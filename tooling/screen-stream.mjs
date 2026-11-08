// A single owned USB (or tcp:HOST private-network) connection to WDA's independent
// MJPEG port. stdout is JPEG stream bytes only; this program never binds a listener
// or touches XCTest.
import http from 'node:http';
import net from 'node:net';

// Device-library diagnostics must never corrupt the binary pipe or expose IDs.
for (const method of ['log', 'info', 'warn', 'error', 'debug']) console[method] = () => {};
const [udid, portArgument = '9100'] = process.argv.slice(2);
const networkHost = udid?.startsWith('tcp:') ? udid.slice(4) : null;
const validTarget = networkHost !== null
  ? /^(?:[A-Za-z0-9][A-Za-z0-9.-]{0,252}|[0-9A-Fa-f:]{2,39})$/.test(networkHost)
  : /^[A-Za-z0-9-]{8,64}$/.test(udid ?? '');
if (!validTarget || !/^\d+$/.test(portArgument)) process.exit(1);
const devicePort = Number(portArgument);
if (!Number.isInteger(devicePort) || devicePort < 1024 || devicePort > 65535) process.exit(1);

let socket, request, response, agent;
let stopping = false;
const startup = setTimeout(() => stop(1), 5000);
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  clearTimeout(startup);
  response?.destroy();
  request?.destroy();
  socket?.destroy();
  agent?.destroy();
  process.exitCode = code;
  setTimeout(() => process.exit(code), 100).unref();
}
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
process.stdout.on('error', () => stop());

try {
  if (networkHost !== null) {
    socket = await new Promise((resolve, reject) => {
      const tcp = net.connect({host: networkHost, port: devicePort});
      tcp.once('connect', () => resolve(tcp));
      tcp.once('error', reject);
    });
  } else {
    const {default: iosDevice} = await import('appium-ios-device');
    socket = await iosDevice.utilities.connectPort(udid, devicePort);
  }
  if (stopping) socket.destroy();
  else {
    socket.setNoDelay?.(true);
    socket.on('error', () => stop(1));
    // Node's HTTP parser removes chunked-transfer framing before writing stdout.
    // This agent always returns the already connected USB socket: no DNS/TCP hop.
    agent = new http.Agent({keepAlive: false});
    agent.createConnection = () => socket;
    request = http.request({host: '127.0.0.1', port: devicePort, path: '/',
      method: 'GET', agent, headers: {Accept: 'multipart/x-mixed-replace', Connection: 'close'}}, incoming => {
      response = incoming;
      clearTimeout(startup);
      if (incoming.statusCode !== 200) { stop(1); return; }
      incoming.on('error', () => stop(1));
      incoming.on('end', () => stop(1));
      // pipe pauses the response when stdout is full, bounding backpressure.
      incoming.pipe(process.stdout, {end: false});
    });
    // usbmux unpipes its plist reader before handing this socket over, leaving
    // it paused. Resume only after HTTP has attached its response parser; a
    // newly created TCP socket fixture would otherwise hide this USB behavior.
    request.on('socket', assigned => assigned.resume());
    request.setTimeout(5000, () => stop(1));
    request.on('error', () => stop(1));
    request.end();
  }
} catch { stop(1); }
