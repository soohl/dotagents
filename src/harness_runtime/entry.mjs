import { mkdir, copyFile, writeFile, constants } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { updateProfile } from './apply-policy.mjs';

const home = process.env.DSH_HOME;
const role = process.env.HARNESS_ROLE;
if (!['chat', 'agent'].includes(role)) throw new Error('HARNESS_ROLE must be chat or agent.');
if (role === 'chat' && process.env.DOTAGENTS_THINGS_TOKEN) {
  await writeFile('/tmp/dotagents-things-token', process.env.DOTAGENTS_THINGS_TOKEN, { mode: 0o600 });
}
if (role === 'chat' && process.env.DOTAGENTS_CALENDAR_ENABLED === 'true'
    && process.env.DOTAGENTS_CALENDAR_TOKEN) {
  await writeFile('/tmp/dotagents-calendar-token', process.env.DOTAGENTS_CALENDAR_TOKEN, { mode: 0o600 });
}
await mkdir(`${home}/profiles/web`, { recursive: true });
await copyFile(`/run/dotagents/${role}-profile.json`, `${home}/profiles/web/cordis.patch.yml`, constants.COPYFILE_EXCL)
  .catch(error => { if (error.code !== 'EEXIST') throw error; });
await updateProfile(`${home}/profiles/web/cordis.patch.yml`, '/app/policy.json', role);
const child = spawn('/app/node_modules/.bin/dsh', ['web', '--no-open'], { stdio: 'inherit' });
for (const name of ['SIGTERM', 'SIGINT']) process.on(name, () => child.kill(name));
child.on('exit', code => process.exit(code ?? 1));
