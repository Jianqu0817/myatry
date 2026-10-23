/** Long-lived host-driven Harness composition. Credentials and OS access stay in Python. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { mountNativeServices, mountNativeTools } from './native-tools.mjs';

const root = process.argv[2];
const pending = new Map();
let serial = 0, active;
const out = value => process.stdout.write(JSON.stringify(value)+'\n');
const rpc = (kind, payload) => new Promise((resolve,reject) => {
  const id = ++serial; pending.set(id,{resolve,reject}); out({kind,id,...payload});
});
let packageRequire, packageAnchors;
async function load(name) {
  if(!packageRequire) {
    const dirs = await readdir(join(root,'node_modules','.pnpm'));
    const entry = dirs.find(d=>d.startsWith('@deepseek-ai+dsh-agent-loop'));
    packageRequire = createRequire(join(root,'node_modules','.pnpm',entry,'node_modules','@deepseek-ai','dsh-agent-loop','package.json'));
  }
  let anchor;
  try {anchor=packageRequire.resolve('@deepseek-ai/'+name+'/package.json');}
  catch {
    if(!packageAnchors) {
      packageAnchors=new Map();
      const dirs=await readdir(join(root,'node_modules','.pnpm'));
      for(const dir of dirs.filter(d=>d.startsWith('@deepseek-ai+'))) {
        const base=join(root,'node_modules','.pnpm',dir,'node_modules','@deepseek-ai');
        for(const item of await readdir(base))
          if(!packageAnchors.has(item))packageAnchors.set(item,join(base,item,'package.json'));
      }
    }
    anchor=packageAnchors.get(name);
  }
  const require=createRequire(anchor);
  if(require(anchor).version !== '0.2.1-rc.2') throw Error('harness_version_mismatch');
  return import(pathToFileURL(require.resolve('@deepseek-ai/'+name)).href);
}
const require = createRequire(join(root,'package.json'));
const {Context} = await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href);
const ctx = new Context();
const nativeConfig=process.argv[3]?JSON.parse(await readFile(process.argv[3],'utf8')):{};
let nativeMounted=false;
for(const name of ['dsh-llm','dsh-session','dsh-session-projection','dsh-system-prompt','dsh-tools','dsh-agent','dsh-agent-loop']) {
  const mod = await load(name);
  await ctx.plugin(mod.default, name==='dsh-system-prompt'?{includeRuntimeContext:false}:name==='dsh-agent-loop'?{agents:[],maxParallelToolCalls:1}:{});
}
const {LlmAdapter} = await load('dsh-llm');
class HostAdapter extends LlmAdapter {
  async *stream(options) {
    const result = await rpc('model',{task:active.task,options:{messages:options.messages,tools:options.tools}});
    if(result.error) throw Error(result.error);
    let index=0;
    for(const block of result.blocks) {
      yield {type:'block-start',index,blockType:block.type};
      if(block.type==='text') yield {type:'text-delta',index,text:block.text};
      if(block.type==='tool-call') yield {type:'tool-call-delta',index,id:block.id,name:block.name,argumentsDelta:block.arguments};
      yield {type:'block-end',index,block}; index++;
    }
    if(result.usage) yield {type:'usage',usage:result.usage};
    yield {type:'finish',reason:{kind:result.blocks.some(b=>b.type==='tool-call')?'tool-calls':'stop'}};
  }
}
ctx.llm.registerAdapter(['myatri-host'],new HostAdapter());
// rc.2 constructs an initial marker before session/event publication attaches.
// Capture that bounded prefix as well: replay requires contiguous events from seq 0.
ctx.on('session/created',session=>{
  for(const event of session.snapshotEvents(active?.events?.length||0,session.seq))
    out({kind:'event',task:active?.task,event});
});
ctx.on('session/event',(session,event)=>out({kind:'event',task:active?.task,event}));
async function run(command) {
  if(active) {out({kind:'error',task:command.task,error:'desktop_busy'});return;}
  active=command;
  let handle;
  try {
    if(command.profile==='full'&&!nativeMounted) {
      await mountNativeServices(ctx,load,nativeConfig);nativeMounted=true;
    }
    handle = await ctx.agents.create({sessionId:command.task,seed:command.events||[],
      ...(command.cwd?{meta:{cwd:command.cwd}}:{}),
      agentOptions:{provider:'myatri-host',model:'host-budgeted'},
      async setup(agentCtx){
        if(command.profile==='full') {
          await mountNativeTools(agentCtx,load,nativeConfig);
          // All native effects pass the durable host claim before dispatch.
          agentCtx.on('tools/execute',async(exec,next)=>{
            if(command.tools.some(t=>t.name===exec.name))return next();
            const admitted=await rpc('native_claim',{task:command.task,name:exec.name,args:exec.arguments,call_id:exec.callId});
            if(admitted.cached)return admitted.cached;
            if(!admitted.allow)throw Error('host_stopped_or_call_unknown');
            const result=await next();
            await rpc('native_result',{task:command.task,call_id:exec.callId,result});
            return result;
          });
          const {scopeOf}=await load('dsh-scope');
          await rpc('catalog',{task:command.task,tools:agentCtx.tools.schemas(scopeOf(agentCtx))});
        }
        agentCtx.systemPrompt.section({name:'myatri-executor',order:0,text:command.prompt,interpolate:false});
        for(const tool of command.tools) agentCtx.tools.register({...tool,
          output:{schema:{},render:(_args,result)=>[{type:'text',text:JSON.stringify(result)}]},
          execute:async(args,execution)=>{
            const result=await rpc('tool',{task:command.task,name:tool.name,args,call_id:execution.callId});
            if(result.end_turn) execution.concludeTurn();
            return result;
          }});
      }});
    active.handle=handle;
    handle.agent.followup({id:command.message_id,role:'user',source:{kind:'user'},content:[{type:'text',text:command.goal}]});
    await handle.agent.whenIdle();
  } catch {command.failed=true;}
  finally {
    if(handle)await handle.dispose();active=undefined;
    out({kind:command.failed?'error':'done',task:command.task,...(command.failed?{error:'harness_turn_failed'}:{})});
  }
}
createInterface({input:process.stdin,crlfDelay:Infinity}).on('line',line=>{
  try {
    const command=JSON.parse(line);
    if(command.kind==='reply') {const p=pending.get(command.id);if(p){pending.delete(command.id);p.resolve(command.result);}}
    else if(command.kind==='run') void run(command);
    else if(command.kind==='cancel'&&active?.task===command.task) active.handle?.agent.cancel({kind:'user',reason:'host_cancelled'});
  }catch{out({kind:'error',error:'invalid_ipc'});}
});
out({kind:'ready',version:'0.2.1-rc.2'});
