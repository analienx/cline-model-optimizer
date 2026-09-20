import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
const here=path.dirname(fileURLToPath(import.meta.url));
const script=fs.readFileSync(path.resolve(here,'../../python/cmo/web/simple.js'),'utf8');
const match=script.match(/function accountSetupCommand\(id,stage\)\{[\s\S]*?\n\}/);
assert.ok(match,'Account command builder must exist in dashboard code');
const build=runInNewContext(`${match[0]}; accountSetupCommand`);
test('sign-in command copies the exact scoped account helper without interpolation placeholders',()=>{
 const cmd=build('account-4','signin');
 assert.equal(cmd,'powershell -NoProfile -File "C:\\Workspace\\repos\\config\\tools\\pi\\Initialize-PiClineAccount.ps1" -Account account-4');
 assert.ok(!cmd.includes('${helper}'));
});
test('new profile setup targets only its registered account',()=>{
 assert.equal(build('account-5','provision'),'powershell -NoProfile -File "C:\\Workspace\\repos\\config\\tools\\pi\\Provision-PiClineAccount.ps1" -Account account-5');
});
test('malformed account identifiers cannot be included in a copied command',()=>{
 assert.throws(()=>build('account-4; Remove-Item C:\\','signin'),/Invalid account slot/);
});
