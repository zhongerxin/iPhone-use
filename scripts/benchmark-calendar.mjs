// Same-model, observable host-loop comparison. Only setup/audit/cleanup use AX.
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createInterface } from 'node:readline';
import { mkdirSync, readFileSync, writeFileSync, appendFileSync, unlinkSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ChatGPTAuth } from '../server/midscene/chatgpt-auth.mjs';
import { createChatGPTClient } from '../server/midscene/chatgpt-client.mjs';

process.umask(0o077);
const exec = promisify(execFile);
const repo = fileURLToPath(new URL('../', import.meta.url));
const root = resolve(process.argv[2]);
mkdirSync(root, {recursive:true,mode:0o700});
const resume = process.argv.includes('--resume');
const orderArg = process.argv.find(arg => arg.startsWith('--order='));
const order = orderArg ? orderArg.slice(8).split(',') : resume ? JSON.parse(readFileSync(join(root,'protocol.json'),'utf8')).order : ['steps','balanced','compact','compact','balanced','steps'];
if (!order.length || order.some(mode => !['steps','balanced','compact','default'].includes(mode))) throw new Error('Invalid benchmark order');
if (existsSync(join(root,'results.json')) && !resume) throw new Error('Use a fresh output directory or --resume');
const fixture = async (...args) => JSON.parse((await exec('python3',[join(repo,'scripts/calendar-benchmark-fixture.py'),...args],{timeout:60000})).stdout);
const auth = new ChatGPTAuth(join(process.env.HOME,'.local/share/iphone-use/chatgpt'));
const model = (await auth.models()).find(m=>/^gpt-/.test(m.slug)).slug;
const tasks = [
  "Starting from this blank new-event form in Calendar (October 10, 2026, initially 19:00–20:00), create exactly one local event titled 'Midscene 对比验证', from 15:00 to 15:30 on October 10, 2026, location exactly '会议室 A'. Save it and reopen the saved event details to verify the title, date, time range and location. Setup has confirmed that the default blue calendar '日历' is a writable local calendar under '我的 iPhone'. Use the current device timezone. Do not invite anyone, sign in, modify existing events or create duplicates. Report the observed saved details and unverified conditions.",
  "Edit the currently open saved event 'Midscene 对比验证' on October 10, 2026. Move its start from 15:00 to 16:00, keeping exactly 30 minutes duration so the end is 16:30. Preserve title, date, location '会议室 A', calendar and all other fields. Save, return to the day calendar and reopen this same saved event to verify the updated details. Do not create any event or invite anyone. Report the observed saved details and unverified conditions."
];
const state=join(root,'state');mkdirSync(join(state,'chatgpt'),{recursive:true,mode:0o700});
const child=spawn('python3',[join(repo,'scripts/benchmark-mcp-bridge.py'),state],{stdio:['pipe','pipe','inherit'],env:{...process.env,IPHONE_USE_MODEL_METRICS:'1'}});
const lines=createInterface({input:child.stdout});let pending;
lines.on('line',line=>{const p=pending;pending=undefined;try{p?.resolve(JSON.parse(line));}catch(e){p?.reject(e);}});
child.on('exit',()=>pending?.reject(new Error('MCP transport closed')));
const raw=(name,args)=>new Promise((resolve,reject)=>{if(pending)return reject(new Error('Concurrent device call'));pending={resolve,reject};child.stdin.write(JSON.stringify({name,arguments:args})+'\n');});
const trials=resume ? JSON.parse(readFileSync(join(root,'results.json'),'utf8')) : [];
if (resume && JSON.parse(readFileSync(join(root,'protocol.json'),'utf8')).model !== model) throw new Error('Model changed; cannot resume');
if (!resume) writeFileSync(join(root,'protocol.json'),JSON.stringify({model,tasks,order,phase_deadline_seconds:300,phase_max_actions:24,endpoint:'Each phase ends at its model-generated final answer after reopening saved details. Independent AX audit, setup, auth preflight and cleanup excluded equally. No automatic retry of failed trials. Host controller is observable, not ChatGPT desktop scheduling.'},null,2));
try {
  let ready=await raw('pua_ready',{recover:true,screenshot:false});
  if(!ready.ready) ready=await raw('pua_ready',{recover:true,screenshot:false});
  if(!ready.ready)throw new Error('Simulator not ready');
  await raw('pua_midscene',{action:'settings',mode:'ai'});
  for(const [index,mode] of order.entries()){
    if (index < trials.length) continue;
    const initial=await fixture('prepare');
    const id=`calendar-${index+1}-${mode}`;
    const trial={id,mode,model,initial,phases:[],success:false,started_at:new Date().toISOString()};
    console.log(JSON.stringify({type:'start',id}));
    for(const [phaseIndex,task] of tasks.entries()){
      // Refresh only the plugin's own grant; no host credentials are read.
      await auth.accessToken();const record=await auth.read('account.json');
      writeFileSync(join(state,'chatgpt/account.json'),JSON.stringify(Object.fromEntries(['access_token','client_id','subject','scopes','expires_at'].map(k=>[k,record[k]]))),{mode:0o600});
      const phase={index:phaseIndex,task,tools:[],model_requests:[],decisions:[]};trial.phases.push(phase);
      const start=performance.now();
      const controller=new AbortController();const timer=setTimeout(()=>controller.abort(new Error('Phase deadline')),300000);
      const log=entry=>appendFileSync(join(root,'events.jsonl'),JSON.stringify({trial:id,phase:phaseIndex,at_ms:performance.now()-start,...entry})+'\n');
      const call=async args=>{
        controller.signal.throwIfAborted();const began=performance.now();
        const result=await raw('pua_midscene',{...args,report_id:id});
        const item={action:args.action,ms:performance.now()-began,error:result.mcp_is_error};phase.tools.push(item);
        phase.last_result=result;phase.model_requests.push(...(result.model_requests??result.error?.model_requests??[]));log({type:'tool',...item});
        if(result.mcp_is_error)throw new Error(result.error?.reason??result.error?.code??'tool failed');
        return result;
      };
      try{
        if(mode!=='steps'){
          const result=await call({action:'act',...(mode === 'default' ? {} : {planning:mode}),text:task});phase.summary=result.completion?.summary;
        }else{
          const client=await createChatGPTClient(auth,{signal:controller.signal,onMetrics:m=>{phase.model_requests.push(m);log({type:'model',...m});}})();
          const history=[];let obs=await call({action:'screenshot'});
          for(let n=0;n<=24;n++){
            controller.signal.throwIfAborted();const im=obs.image;
            const prompt=`Control this iPhone from screenshots, one explicit action each turn. Include an "observed" string with short new task-relevant facts and verified progress. Return only JSON using one of: {"action":"tap","x":number,"y":number}, {"action":"swipe","x":number,"y":number,"end_x":number,"end_y":number}, {"action":"input","text":"single line to append"}, {"action":"finish","summary":"observed saved details and unverified conditions"}. Coordinates are device points in ${obs.viewport.width}x${obs.viewport.height}, screenshot pixel-to-point factors ${JSON.stringify(im.pixel_to_point)}. Input appends text at the current focus; never repeat input just because it is clipped. Inspect each new screenshot, do not repeat unchanged taps. Stop for authentication, never enter credentials. Finish only after the required saved-event reopen and verification. Task: ${task}\nHistory: ${JSON.stringify(history)}`;
            const answer=await client.chat.completions.create({model,messages:[{role:'user',content:[{type:'text',text:prompt},{type:'image_url',image_url:{url:`data:${im.mimeType};base64,${readFileSync(im.path).toString('base64')}`,detail:'original'}}]}]});
            const decision=JSON.parse(answer.choices[0].message.content.replace(/^```(?:json)?\s*|\s*```$/g,''));phase.decisions.push(decision);log({type:'decision',decision});
            if(decision.action==='finish'){phase.summary=decision.summary;break;}
            if(n===24)throw new Error('cycle_limit');
            if(!['tap','swipe','input'].includes(decision.action))throw new Error('Unsupported controller action');
            history.push(decision);
            const allowed=decision.action==='input'?['action','text']:decision.action==='tap'?['action','x','y']:['action','x','y','end_x','end_y'];
            obs=await call(Object.fromEntries(Object.entries(decision).filter(([k])=>allowed.includes(k))));
          }
        }
        controller.signal.throwIfAborted();phase.wall_ms=performance.now()-start;
        phase.audit=await fixture('audit',...(phaseIndex===0?['15:00','15:30']:['16:00','16:30']));
        phase.success=phase.audit.passed && Boolean(phase.summary);
      }catch(e){phase.wall_ms=performance.now()-start;phase.success=false;phase.error=e.message;}
      finally{clearTimeout(timer);}
      phase.model_ms=phase.model_requests.reduce((sum,m)=>sum+m.total_ms,0);
      writeFileSync(join(root,`${id}-phase-${phaseIndex}.json`),JSON.stringify(phase,null,2));
      console.log(JSON.stringify({type:'phase',id,index:phaseIndex,success:phase.success,seconds:phase.wall_ms/1000,requests:phase.model_requests.length,error:phase.error}));
      if(!phase.success)break;
    }
    trial.wall_ms=trial.phases.reduce((s,p)=>s+p.wall_ms,0);trial.success=trial.phases.length===2&&trial.phases.every(p=>p.success);
    trials.push(trial);writeFileSync(join(root,'results.json'),JSON.stringify(trials,null,2));
    const cleaned=await fixture('cleanup');trial.cleanup=cleaned;
    writeFileSync(join(root,'results.json'),JSON.stringify(trials,null,2));
    console.log(JSON.stringify({type:'done',id,success:trial.success,seconds:trial.wall_ms/1000,cleaned}));
  }
}finally{
  child.stdin.end();try{unlinkSync(join(state,'chatgpt/account.json'));}catch{}
  writeFileSync(join(root,'results.json'),JSON.stringify(trials,null,2));
}
