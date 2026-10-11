import test from 'node:test';
import assert from 'node:assert/strict';
import { harnessSchema } from '../src/harness_runtime/things-schema.mjs';

test('inline local definitions and nullable fields while retaining server validation guidance',()=>{
  const source={$defs:{Target:{type:'object',properties:{id:{type:'string',minLength:1}},required:['id'],additionalProperties:false}},
    type:'object',properties:{target:{anyOf:[{$ref:'#/$defs/Target'},{type:'null'}]}}};
  const schema=harnessSchema(source);
  assert.equal(schema.$defs,undefined);
  const target=schema.properties.target.oneOf[0];
  assert.deepEqual(target.required,['id']); assert.equal(target.additionalProperties,false);
  assert.match(target.properties.id.description,/minLength: 1/);
  assert.ok(source.$defs);
});
test('overlapping scalar alternatives do not reject values accepted by the backend',()=>{
  const schema=harnessSchema({anyOf:[{type:'string',format:'date'},{type:'string',const:''},{type:'null'}]});
  assert.deepEqual(schema.oneOf.map(s=>s.type),['string','null']);
  assert.throws(()=>harnessSchema({$ref:'https://example.com/schema'}));
  assert.throws(()=>harnessSchema({$defs:{cycle:{$ref:'#/$defs/cycle'}},$ref:'#/$defs/cycle'}));
});
