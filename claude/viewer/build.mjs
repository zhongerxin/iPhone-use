import { build } from 'esbuild';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const viewerRoot = new URL('./', import.meta.url);
const output = await build({
  entryPoints: [fileURLToPath(new URL('host.ts', viewerRoot))],
  bundle: true,
  platform: 'browser',
  target: 'es2022',
  format: 'esm',
  minify: true,
  // SDK 传输层的调试日志包含完整的 base64 画面帧，发布包中去掉 debug 日志。
  pure: ['console.debug'],
  write: false,
  legalComments: 'inline',
});
const html = await readFile(new URL('host.html', viewerRoot), 'utf8');
// 用回调替换，保留打包产物中字面的 $` 与 $' 序列。
const page = html.replace('/*__SCRIPT__*/', () => output.outputFiles[0].text.replace(/<\/script/gi, '<\\/script'));
await writeFile(new URL('viewer.html', viewerRoot), page);
console.log('Built self-contained viewer.html.');
