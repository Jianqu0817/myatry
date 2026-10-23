/** Official Harness host capabilities; no application/path allowlist in full mode. */
export async function mountNativeServices(ctx, load, config={}) {
  for(const [name,options] of [
    ['dsh-subprocess-local',{}],
    ['dsh-fs-local',{}],
    ['dsh-working-directory',{}],
    ['dsh-shell-env',{}],
    ['dsh-pwsh-local',{timeoutMs:30000,maxTimeoutMs:600000,maxOutputBytes:32000,maxSpillBytes:4000000}],
    ['dsh-jobs-local',{maxConcurrentJobsPerOwner:10}],
    ['dsh-skill',{}],
  ]) {
    const mod=await load(name);
    await ctx.plugin(mod.default||mod,options);
  }
  for(const skill of config.skills||[]) ctx.skills.register(skill);
}

export async function mountNativeTools(ctx, load, config={}) {
  for(const [name,options] of [
    ['dsh-tool-fs',{readImage:false,readMaxBytes:32000}],
    ['dsh-tool-fs-search',{sampleOverCapGlobResults:true}],
    ['dsh-tool-pwsh',{enableRunInBackground:true,promoteOnTimeout:true}],
    ['dsh-tool-jobs',{completionDelivery:'quiet',waitTimeoutMs:1000,maxWaitTimeoutMs:30000}],
    ['dsh-tool-working-directory',{}],
    ['dsh-tool-todo',{allowParallelInProgress:true}],
    ['dsh-tool-skill',{}],
  ]) {
    const mod=await load(name);
    await ctx.plugin(mod.default||mod,options);
  }
  for(const server of config.mcpServers||[]) {
    const mod=await load('dsh-mcp-client');
    await ctx.plugin(mod,{...server,toolCallTimeoutMs:server.toolCallTimeoutMs||60000,failOnStartupError:true});
  }
}
