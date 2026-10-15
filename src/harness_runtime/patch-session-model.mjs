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
  // Keep the durable selection, but display only models in the live catalog.
  ['const showSearch = choices.length > 4;', `const noModels = choices.length === 0 && state.status === "ready";
\t\t\t(0, react.useEffect)(() => { if (noModels) setOpen(false); }, [noModels]);
\t\t\tconst showSearch = choices.length > 4;`],
  ['const effortLabel = reasoning === void 0 ?', 'const effortLabel = currentChoice === void 0 ? void 0 : reasoning === void 0 ?'],
  ['const waiting = state.current === null && state.status === "loading";',
   'const waiting = choices.length === 0 && state.status === "loading";'],
  ['const modelLabel = waiting ? t("trigger.loading") : currentChoice?.model.name ?? (state.current === null ? t("trigger.fallback") : `${state.current.provider}/${state.current.model}`);',
   'const modelLabel = waiting ? t("trigger.loading") : noModels ? t("empty.models") : currentChoice?.model.name ?? t("trigger.fallback");'],
  ['const triggerAria = waiting ?', 'const triggerAria = noModels ? modelLabel : waiting ?'],
  ['title: triggerLabel,', 'title: noModels ? modelLabel : locked ? "Start a new conversation to use another model." : triggerLabel,'],
  ['disabled: locked,', 'disabled: locked || noModels,'],
  ['open && (0, react_dom.createPortal)', '!noModels && open && (0, react_dom.createPortal)'],
  ['"empty.models": "No models available."', '"empty.models": "Inference engine is not running"'],
  ['"empty.models": "没有可用的模型。"', '"empty.models": "推理引擎未运行"'],
]);
