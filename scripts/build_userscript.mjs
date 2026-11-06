import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Script } from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2), portIndex = args.indexOf('--api-port'), outputIndex = args.indexOf('--output');
const port = portIndex < 0 ? 8765 : Number(args[portIndex + 1]);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid API port');
const output = outputIndex < 0 ? resolve(root, 'dist/plamo-for-cp.user.js') : resolve(args[outputIndex + 1]);
const api = `http://127.0.0.1:${port}`;
const core = (await readFile(resolve(root, 'browser/translator.js'), 'utf8')).replace("const API = 'http://127.0.0.1:8765';", `const API = '${api}';`);
const rules = (await readFile(resolve(root, 'browser/site-rules.js'), 'utf8')).replaceAll('export function ', 'function ');
const registry = (await readFile(resolve(root, 'browser/site-registry.js'), 'utf8')).replaceAll('export const ', 'const ').replaceAll('export function ', 'function ');
const settings = await readFile(resolve(root, 'browser/settings.js'), 'utf8');
const jobs = await readFile(resolve(root, 'plamo_cp/assets/translation-jobs.js'), 'utf8');
const shim = (await readFile(resolve(root, 'userscript/shim.js'), 'utf8')).replaceAll('__PLAMO_API__', api)
  .replace('  __PLAMO_SITE_RULES__', () => rules)
  .replace('  __PLAMO_SITE_REGISTRY__', () => registry)
  .replace('  __PLAMO_BROWSER_SETTINGS__', () => settings)
  .replace('  __PLAMO_TRANSLATION_JOBS__', () => jobs)
  .replace('      __PLAMO_TRANSLATOR__', () => core);
const metadata = (await readFile(resolve(root, 'userscript/metadata.txt'), 'utf8')).replaceAll('__PLAMO_API__', api);
const source = metadata + '\n' + shim;
new Script(source, { filename: 'plamo-for-cp.user.js' });
await mkdir(dirname(output), { recursive: true });
await writeFile(output, source);
console.log(`Tampermonkey script: ${output}`);
