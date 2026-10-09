import { build } from '../../ui/node_modules/esbuild/lib/main.js';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const outputPath = process.argv[2];
if (!outputPath) throw new Error('Usage: node scripts/android-preview/build.mjs OUTPUT.html');
const bundle = await build({
  entryPoints: [fileURLToPath(new URL('../../ui/src/app.ts', import.meta.url))],
  alias: { '@modelcontextprotocol/ext-apps': fileURLToPath(new URL('app.ts', import.meta.url)) },
  bundle: true, platform: 'browser', target: 'es2022', format: 'esm', write: false,
});
const html = await readFile(new URL('../../ui/index.html', import.meta.url), 'utf8');
const css = await readFile(new URL('../../ui/style.css', import.meta.url), 'utf8');
await writeFile(outputPath, html.replaceAll('iPhone', 'Android').replace('<main id="app"', '<main id="app" data-platform="android"')
  .replace('/*__STYLE__*/', () => css + '\n.dynamic-island,.side-button,.receiver{display:none!important}\n#screen{border-radius:0}')
  .replace('/*__SCRIPT__*/', () => bundle.outputFiles[0].text.replace(/<\/script/gi, '<\\/script')));
console.log(outputPath);
