import japanese from '../../locales/ja/ui.json';

declare const __IPHONE_USE_LANGUAGE__: 'default' | 'ja';
const strings: Record<string, string> = japanese.strings;

export const localize = (text: string): string =>
  __IPHONE_USE_LANGUAGE__ === 'ja' ? strings[text] ?? text : text;
