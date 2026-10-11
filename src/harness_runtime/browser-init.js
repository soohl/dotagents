// Playwright's supported init-script hook. Observe WebGL without changing results.
(() => {
  const state = { drawCalls: 0, shaderErrors: [], programErrors: [] };
  Object.defineProperty(window, '__dotagentsBrowser', { value: state });
  const record = (list, message) => {
    if (list.length < 20) list.push(message);
    console.error('[WebGL verification]', message);
  };
  for (const prototype of [window.WebGLRenderingContext?.prototype,
    window.WebGL2RenderingContext?.prototype].filter(Boolean)) {
    for (const name of ['compileShader', 'linkProgram', 'drawArrays', 'drawElements',
      'drawArraysInstanced', 'drawElementsInstanced']) {
      const original = prototype[name];
      if (!original) continue;
      prototype[name] = function (...args) {
        const result = original.apply(this, args);
        if (name === 'compileShader' && !this.getShaderParameter(args[0], this.COMPILE_STATUS))
          record(state.shaderErrors, this.getShaderInfoLog(args[0]) || 'Shader compilation failed.');
        if (name === 'linkProgram' && !this.getProgramParameter(args[0], this.LINK_STATUS))
          record(state.programErrors, this.getProgramInfoLog(args[0]) || 'Shader linking failed.');
        if (name.startsWith('draw')) state.drawCalls++;
        return result;
      };
    }
  }
})();
