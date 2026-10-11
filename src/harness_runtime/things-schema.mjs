// Harness accepts a narrower schema vocabulary. Things still validates every call.
const supported = new Set(['type','oneOf','properties','required','additionalProperties','items',
  'enum','const','description','title','default','examples']);
const limits = new Set(['minimum','maximum','minLength','maxLength','minItems','maxItems','pattern','format']);

export function harnessSchema(root) {
  function convert(schema, chain = []) {
    if (typeof schema === 'boolean') return schema;
    if (!schema || typeof schema !== 'object' || Array.isArray(schema)) throw new Error('Invalid Things tool schema');
    if (schema.$ref) {
      const ref = schema.$ref;
      if (!ref.startsWith('#/$defs/') || chain.includes(ref)) throw new Error('Unsupported Things schema reference');
      const name = ref.slice(8).replace(/~1/g,'/').replace(/~0/g,'~');
      const target = root.$defs?.[name];
      if (!target) throw new Error('Missing Things schema definition');
      const { $ref, ...rest } = schema;
      return convert({...target,...rest}, [...chain,ref]);
    }
    const out = {};
    const notes = [];
    for (const [key,value] of Object.entries(schema)) {
      if (key === '$defs' || key === 'discriminator') continue;
      if (limits.has(key)) { notes.push(`${key}: ${JSON.stringify(value)}`); continue; }
      if (key === 'anyOf' || key === 'oneOf') {
        let branches = value.map(item => convert(item,chain));
        if (key === 'anyOf') {
          // After constraints move into descriptions, e.g. date-string OR ""
          // becomes string OR string. A broad scalar branch already covers both.
          branches = branches.filter((item,index) => !branches.some((other,j) => j !== index
            && other.type === item.type && ['string','number','integer','boolean','null'].includes(other.type)
            && other.enum === undefined && other.const === undefined
            && (item.enum !== undefined || item.const !== undefined || j < index)));
        }
        out.oneOf = branches;
      } else if (key === 'properties') {
        out.properties = Object.fromEntries(Object.entries(value).map(([k,v]) => [k,convert(v,chain)]));
      } else if (key === 'items' || key === 'additionalProperties' && typeof value === 'object') {
        out[key] = convert(value,chain);
      } else if (supported.has(key)) out[key] = value;
      else throw new Error(`Unsupported Things schema keyword: ${key}`);
    }
    if (notes.length) out.description = [out.description, 'Server validation: '+notes.join('; ')+'.'].filter(Boolean).join(' ');
    return out;
  }
  return convert(root);
}
