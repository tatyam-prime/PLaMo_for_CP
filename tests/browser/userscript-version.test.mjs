import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../../plamo_cp/assets/userscript-version.js', import.meta.url), 'utf8');
const compare = runInNewContext(source + '\ncompareUserscriptVersions;');

test('userscript versions compare numeric components and treat missing trailing zeros as equal', () => {
  assert.equal(compare('0.4.9', '0.4.10'), -1);
  assert.equal(compare('0.4.10', '0.4.9'), 1);
  assert.equal(compare('0.4.16', '0.4.16'), 0);
  assert.equal(compare('1.0', '1.0.0.0'), 0);
  assert.equal(compare('0.99.99', '1'), -1);
  assert.equal(compare('1.01', '1.1'), 0);
});

test('unknown userscript versions are never reported as an available update', () => {
  for (const invalid of [undefined, null, 16, {}, [], '', 'v1.2.3', '1.2.3-beta', '1.2.3.4.5', ' 1.2', '1.2\n', '-1.2', '1..2', '9007199254740992', '9'.repeat(33), 'https://example.org/update', '<script>alert(1)</script>']) {
    assert.equal(compare(invalid, '1.2.3'), null);
    assert.equal(compare('1.2.3', invalid), null);
  }
});
