// Adapt only the pinned adapter's model source. Keep settings and sessions intact.
import { readFile, writeFile } from 'node:fs/promises';
const path = '/app/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js';
let source = await readFile(path, 'utf8');
function replace(before, after) {
  if (source.split(before).length !== 2) throw new Error('Pinned Harness model adapter changed; review live discovery.');
  source = source.replace(before, after);
}
replace('function apply(ctx, config) {', 'function apply(ctx, config) {\n\tconst liveModels = new LiveModels();');
replace('const raw = config.providers.get();', 'const raw = liveModels.providers(config.providers.get());');
replace('\tensureRegistrationFacts();\n\tctx.on("loader/volatile-update",', `\tensureRegistrationFacts();
\tctx.on("dispose", liveModels.start(() => {
\t\tensureRegistrationFacts();
\t\tensureDirectory();
\t\t// Model changes also refresh selectors when provider IDs stay the same.
\t\tif (registration) registration.replace([...profiles().keys()]);
\t}));
\tctx.on("loader/volatile-update",`);
await writeFile(path, 'import { LiveModels } from "/app/runtime/live-models.mjs";\n' + source);
