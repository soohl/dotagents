// Adapt the pinned controller and its client; fail the build if upstream changes.
import { readFile, writeFile } from 'node:fs/promises';

async function patch(path, changes, prefix = '') {
  let source = await readFile(path, 'utf8');
  for (const [before, after] of changes) {
    if (source.split(before).length !== 2) throw new Error(`Pinned Harness session policy changed: ${before}`);
    source = source.replace(before, after);
  }
  await writeFile(path, prefix + source);
}

await patch('/app/node_modules/@deepseek-ai/dsh-api-session-controller/lib/index.js', [
  ['let picked = projectionState.pending === null ? void 0 : agentModelSelection(projectionState.pending);',
   'let picked = projectionState.locked && projectionState.lastUsed !== null ? agentModelSelection(projectionState.lastUsed) : projectionState.pending === null ? void 0 : agentModelSelection(projectionState.pending);'],
  ['\t\t\t\tawait this.requireModel(request);',
   '\t\t\t\tassertSessionModel(this.agents.selectionFor(agent).current, request, this.ctx.sessionProjections.stateOf(agent.session, "modelSelection")?.locked === true, RemoteError);\n\t\t\t\tawait this.requireModel(request);'],
  ['\t\t\t\t\tif (request.mode === "steer") agent.steer(message);',
   '\t\t\t\t\tif (!this.ctx.sessionProjections.stateOf(agent.session, "modelSelection")?.locked) this.agents.selectForNextRequest(agent, { ...this.agents.selectionFor(agent).current });\n\t\t\t\t\tif (request.mode === "steer") agent.steer(message);'],
  ['return hasImage ? this.agents.serializeImageAdmission(agent, admit) : admit();',
   'return this.agents.serializeImageAdmission(agent, admit);'],
  ['const modelSelectionProjectionStateSchema = z$1.object({',
   'const modelSelectionProjectionStateSchema = z$1.object({\n\tlocked: z$1.boolean(),'],
  ['const modelSelectionProjectionSchema = z$1.object({',
   'const modelSelectionProjectionSchema = z$1.object({\n\tlocked: z$1.boolean(),'],
  ['function applyModelSelectionProjection(state, event) {',
   'function applyModelSelectionProjection(state, event) {\n\treturn lockModelProjection(state, advanceModelSelectionProjection(state, event), event);\n}\nfunction advanceModelSelectionProjection(state, event) {'],
  ['\tinit: () => ({\n\t\tlastUsed: null,', '\tinit: () => ({\n\t\tlocked: false,\n\t\tlastUsed: null,'],
  ['\t\tview: (state) => ({\n\t\t\tlastUsed: state.lastUsed,', '\t\tview: (state) => ({\n\t\t\tlocked: state.locked,\n\t\t\tlastUsed: state.lastUsed,'],
  ['\tstateVersion: 2\n};\nfunction sameSelection', '\tstateVersion: 3\n};\nfunction sameSelection'],
], 'import { lockModelProjection, assertSessionModel } from "/app/runtime/session-model-lock.mjs";\n');

await patch('/app/node_modules/@deepseek-ai/dsh-client-ui-conversation/lib/client.js', [
  ['const modelSeatLocked = removed || inert || !live;',
   'const modelSelection = useProjection("modelSelection");\n\t\t\tconst modelSeatLocked = removed || inert || !live || modelSelection?.locked === true || input?.phase === "submitting" || input?.phase === "adjudicating";'],
]);
await patch('/app/node_modules/@deepseek-ai/dsh-client-ui-model-selection/lib/client.js', [
  ['title: triggerLabel,', 'title: locked ? "Start a new conversation to use another model." : triggerLabel,'],
]);
