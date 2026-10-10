import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { writeFile, access } from 'node:fs/promises';
import { dirname } from 'node:path';
import { createConnector, tunnelAddress, discover } from '../tooling/device-transport.mjs';
const udid = 'TEST-DEVICE-1234';
const device = () => ({ hardwareProperties: { udid }, connectionProperties: {
  pairingState: 'paired', tunnelState: 'connected', transportType: 'localNetwork', tunnelIPAddress: 'fd12:3456::1',
} });
test('tunnel discovery accepts only the selected paired device and a connected private IPv6 tunnel', () => {
  assert.equal(tunnelAddress(device(), udid), 'fd12:3456::1');
  for (const [field, value] of [['pairingState','unpaired'], ['tunnelState','disconnected'],
    ['transportType','unknown'], ['tunnelIPAddress','127.0.0.1'], ['tunnelIPAddress','2001:db8::1']]) {
    const d = device(); d.connectionProperties[field] = value;
    assert.throws(() => tunnelAddress(d, udid));
  }
  assert.throws(() => tunnelAddress(device(), 'OTHER-DEVICE'));
});
test('prefers USB on each connection, falls back to Wi-Fi and returns to USB after reconnection', async () => {
  let plugged = true, lookups = 0;
  const usb = {}, wifi = {};
  const connect = createConnector(async () => { if (plugged) return usb; throw Error('unplugged'); },
    async () => { lookups++; return 'fd12::1'; }, async () => wifi);
  assert.equal(await connect(udid,8100),usb); assert.equal(lookups,0);
  plugged = false;
  assert.equal(await connect(udid,8100),wifi); assert.equal(lookups,1);
  assert.equal(await connect(udid,9100),wifi); assert.equal(lookups,1);
  plugged = true;
  assert.equal(await connect(udid,8100),usb);
});
test('failed tunnel connection invalidates cached discovery without retrying the request', async () => {
  let lookups = 0, attempts = 0;
  const connect = createConnector(async () => { throw Error('no USB'); },
    async () => { lookups++; return 'fd12::1'; }, async () => { attempts++; throw Error('offline'); });
  await assert.rejects(connect(udid,8100)); assert.equal(attempts,1);
  await assert.rejects(connect(udid,8100)); assert.equal(attempts,2); assert.equal(lookups,2);
});
test('disposes a late USB socket after bounded fallback', async () => {
  let resolveUSB, destroyed = false;
  const wifi = {};
  const connect = createConnector(() => new Promise(resolve => { resolveUSB = resolve; }),
    async () => 'fd12::1', async () => wifi);
  assert.equal(await connect(udid,8100),wifi);
  resolveUSB({ destroy() { destroyed = true; } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(destroyed,true);
});
test('wireless socket transports bytes and does not reconnect or replay after disconnection', async () => {
  let connections = 0, lookups = 0;
  const server = net.createServer(socket => {
    connections++;
    socket.once('data', data => socket.end(Buffer.concat([Buffer.from('reply:'),data])));
  });
  await new Promise(resolve => server.listen(0,'::1',resolve));
  try {
    const connect = createConnector(async () => { throw Error('no USB'); }, async () => { lookups++; return '::1'; });
    const socket = await connect(udid,server.address().port);
    const chunks = [];
    socket.on('data', chunk => chunks.push(chunk));
    const ended = new Promise(resolve => socket.once('close',resolve));
    socket.write('one-command'); await ended;
    assert.equal(Buffer.concat(chunks).toString(),'reply:one-command');
    assert.equal(connections,1); assert.equal(lookups,1);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('CoreDevice discovery uses a bounded exact-device command and removes temporary output', async () => {
  let output;
  const address = await discover(udid, async (command,args,options) => {
    assert.equal(command,'xcrun');
    assert.deepEqual(args.slice(0,6),['devicectl','device','info','details','--device',udid]);
    assert.equal(options.timeout,3500);
    output = args.at(-1);
    await writeFile(output,JSON.stringify({result:device()}));
  });
  assert.equal(address,'fd12:3456::1');
  await assert.rejects(access(dirname(output)));
});
test('discovery errors and malformed output fail closed and clean up temporary files', async () => {
  for (const kind of ['timeout','invalid-json','wrong-device']) {
    let output;
    await assert.rejects(discover(udid,async (_,args) => {
      output=args.at(-1);
      if(kind==='timeout') throw Error('timeout');
      const wrong=device(); wrong.hardwareProperties.udid='OTHER-DEVICE';
      await writeFile(output,kind==='invalid-json' ? '{' : JSON.stringify({result:wrong}));
    }));
    await assert.rejects(access(dirname(output)));
  }
});
