import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const uiRoot = new URL('./', import.meta.url);
const assetRoot = new URL('../assets/', import.meta.url);
const options = {
  entryPoints: [fileURLToPath(new URL('src/app.ts', uiRoot))],
  bundle: true,
  platform: 'browser',
  target: 'es2022',
  format: 'esm',
  minify: true,
  // The SDK transport debug messages include complete base64 preview frames.
  // Omit only debug logging in the shipped bundle; keep warnings and errors.
  pure: ['console.debug'],
  write: false,
  legalComments: 'inline',
};
const [html, css] = await Promise.all([
  readFile(new URL('index.html', uiRoot), 'utf8'),
  readFile(new URL('style.css', uiRoot), 'utf8'),
]);
const japanese = JSON.parse(await readFile(new URL('../locales/ja/ui.json', uiRoot), 'utf8'));
await mkdir(assetRoot, { recursive: true });
for (const language of ['default', 'ja']) {
  const output = await build({ ...options, define: { __IPHONE_USE_LANGUAGE__: JSON.stringify(language) } });
  let template = html;
  let style = css;
  if (language === 'ja') {
    template = template.replace('lang="zh-CN"', 'lang="ja"');
    for (const [from, to] of Object.entries(japanese.html)) template = template.split(from).join(to);
    style = style.replace('"SF Pro Text","PingFang SC"', '"SF Pro Text","Hiragino Sans","Yu Gothic","PingFang SC"');
  }
  // Callbacks preserve literal $` and $' sequences in the bundled SDK.
  const widget = template
    .replace('/*__STYLE__*/', () => style)
    .replace('/*__SCRIPT__*/', () => output.outputFiles[0].text.replace(/<\/script/gi, '<\\/script'));
  const name = language === 'ja' ? 'phone-screen.ja.html' : 'phone-screen.html';
  await writeFile(new URL(name, assetRoot), widget);
  console.log(`Built self-contained ${name}.`);
}
