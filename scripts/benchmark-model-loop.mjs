// Observable host replacement for a simulator-only, same-model comparison.
// Does not measure ChatGPT desktop scheduling. Never reads host credentials.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdirSync, readFileSync, writeFileSync, appendFileSync, unlinkSync, readdirSync, symlinkSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ChatGPTAuth } from '../server/midscene/chatgpt-auth.mjs';
import { createChatGPTClient } from '../server/midscene/chatgpt-client.mjs';

process.umask(0o077);
const repo = fileURLToPath(new URL('../', import.meta.url));
const root = resolve(process.argv[2]);
mkdirSync(root, { recursive: true, mode: 0o700 });
const variant = process.argv[3] ?? 'baseline';
const fast = ['fast', 'fast-memory'].includes(variant);
let worker;
if (variant === 'fast') {
  worker = join(root, 'worker');
  mkdirSync(worker, {recursive:true});
  const original = join(repo, 'server/midscene');
  for (const file of readdirSync(original)) {
    if (file === 'run-ai.mjs') {
      const code = readFileSync(join(original,file),'utf8');
      const needle = "compact ? compactPlanningContext : ''";
      if (!code.includes(needle)) throw new Error('Unknown worker version');
      writeFileSync(join(worker,file),code.replace(needle,"''"));
    } else {try{symlinkSync(join(original,file),join(worker,file));}catch(e){if(e.code!=='EEXIST')throw e;}}
  }
}
const auth = new ChatGPTAuth(join(process.env.HOME, '.local/share/iphone-use/chatgpt'));
const models = await auth.models();
const model = models.find(m => /^gpt-/.test(m.slug)).slug;
const task = 'In iPhone Settings, starting from General, open About and read the Model Name. Return to General, open Language & Region and read the Region. Finally return to General and report both observed values. Do not change any settings, sign in, or enter credentials. Use screenshots to decide actions and verify the final page. Do not claim a value without visiting its page.';
const state = join(root, 'state');
mkdirSync(join(state, 'chatgpt'), { recursive: true, mode: 0o700 });
const child = spawn('python3', [join(repo, 'scripts/benchmark-mcp-bridge.py'), state, ...(worker?[worker]:[])], {
  stdio: ['pipe', 'pipe', 'inherit'], env: {...process.env, IPHONE_USE_MODEL_METRICS:'1'},
});
const lines = createInterface({ input: child.stdout });
let pending;
lines.on('line', line => { const p = pending; pending = undefined; p?.resolve(JSON.parse(line)); });
child.on('exit', () => pending?.reject(new Error('MCP transport exited')));
const rawTool = (name, args) => new Promise((resolve, reject) => {
  if (pending) return reject(new Error('Concurrent device call'));
  pending = { resolve, reject };
  child.stdin.write(JSON.stringify({ name, arguments: args }) + '\n');
});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const wda = async (path, body) => {
  const res = await fetch('http://127.0.0.1:18101' + path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  const data = await res.json();
  if (!res.ok || data.value?.error) throw new Error('WDA setup failed');
  return data;
};
const status = await wda('/status');
if (!status.value?.ios?.simulatorVersion) throw new Error('Simulator required');
let session = status.sessionId;
const source = async () => (await wda(`/session/${session}/source`)).value;
const general = s => /XCUIElementTypeNavigationBar[^>]*name="通用"/.test(s);
async function reset() {
  for (let i=0;i<4;i++) {
    const screen = await source();
    if (general(screen)) return;
    if (!/XCUIElementTypeNavigationBar[^>]*name="(?:关于本机|语言与地区)"/.test(screen))
      throw new Error('Open Settings > General before running this benchmark');
    await wda(`/session/${session}/wda/tap`, {x:35,y:85});
    await sleep(700);
  }
  throw new Error('Initial General screen not reached');
}
const trials=[];
try {
  let ready = await rawTool('pua_ready', {recover:true,screenshot:false});
  if (!ready.ready) ready = await rawTool('pua_ready', {recover:true,screenshot:false});
  if (!ready.ready) throw new Error('Simulator not ready');
  session = (await wda('/status')).sessionId;
  await rawTool('pua_midscene', {action:'settings',mode:'ai'});
  for (const [index,mode] of (fast ? ['ai','ai','ai'] : ['steps','ai','ai','steps','steps','ai']).entries()) {
    await reset();
    await auth.accessToken();
    const record = await auth.read('account.json');
    const copy = Object.fromEntries(['access_token','client_id','subject','scopes','expires_at'].map(k=>[k,record[k]]));
    writeFileSync(join(state,'chatgpt/account.json'),JSON.stringify(copy),{mode:0o600});
    const id=`instrumented-${index+1}-${mode}`;
    const trial={id,mode,variant,effort:fast?'fast':'balance',model,task,tools:[],model_requests:[],decisions:[],started_at:new Date().toISOString()};
    const began=performance.now();
    const log = data => appendFileSync(join(root,'events.jsonl'),JSON.stringify({trial:id,at_ms:performance.now()-began,...data})+'\n');
    const tool=async args=>{
      const t=performance.now(); const result=await rawTool('pua_midscene',{...args,report_id:id});
      const call={action:args.action,ms:performance.now()-t,error:result.mcp_is_error};
      trial.tools.push(call); log({type:'tool',...call});
      trial.last_result=result;
      const requests = result.model_requests ?? result.error?.model_requests;
      if (requests) trial.model_requests.push(...requests);
      if(result.mcp_is_error) throw new Error(result.error?.reason ?? result.error?.code ?? 'MCP tool failed');
      return result;
    };
    console.log(JSON.stringify({type:'start',id,model}));
    try {
      if(mode==='ai') {
        const result=await tool({action:'act',text:task,planning:fast?'compact':'balanced'});
        trial.summary=result.completion?.summary;
      } else {
        const client=await createChatGPTClient(auth,{onMetrics:m=>{trial.model_requests.push(m);log({type:'model',...m});}})();
        const history=[];
        let observation=await tool({action:'screenshot'});
        for(let n=0;n<20;n++) {
          const image=observation.image;
          const instruction=`You control an iPhone using one explicit action per turn. Inspect the current screenshot and previous action history. Include an "observed" string with any task-relevant values visible now, to remember them after leaving the page. Return ONLY JSON: {"action":"tap","x":number,"y":number}, {"action":"swipe","x":number,"y":number,"end_x":number,"end_y":number}, or {"action":"finish","summary":"observed values and final page"}. Coordinates are iPhone points in ${observation.viewport.width}x${observation.viewport.height}; screenshot pixels map by ${JSON.stringify(image.pixel_to_point)}. Never guess unseen values. Stop if authentication is needed. Only finish after all requested visits and final verification. Task: ${task}\nHistory: ${JSON.stringify(history)}`;
          const response=await client.chat.completions.create({model,messages:[{role:'user',content:[{type:'text',text:instruction},{type:'image_url',image_url:{url:`data:${image.mimeType};base64,${readFileSync(image.path).toString('base64')}`,detail:'original'}}]}]});
          const text=response.choices[0].message.content;
          const decision=JSON.parse(text.replace(/^```(?:json)?\s*|\s*```$/g,''));
          trial.decisions.push(decision); log({type:'decision',decision});
          if(decision.action==='finish'){trial.summary=decision.summary;break;}
          if(!['tap','swipe'].includes(decision.action))throw new Error('Unallowed benchmark action');
          // Preserve observations needed after navigating away, without another model call.
          history.push({decision,screen: n});
          // The observation notes above retain read values after navigating away.
          const action = Object.fromEntries(Object.entries(decision).filter(([key])=>['action','x','y','end_x','end_y'].includes(key)));
          observation=await tool(action);
          if(n===19)throw new Error('Step budget exceeded');
        }
      }
      trial.wall_ms=performance.now()-began;
      trial.final_general=general(await source());
      trial.success=trial.final_general && /iPhone\s*17\s*Pro/i.test(trial.summary??'') && /中国|China/.test(trial.summary??'');
    } catch(error) {trial.wall_ms=performance.now()-began;trial.success=false;trial.error=error.message;}
    trial.model_ms=trial.model_requests.reduce((s,m)=>s+m.total_ms,0);
    trial.tool_ms=trial.tools.reduce((s,m)=>s+m.ms,0);
    writeFileSync(join(root,id+'.json'),JSON.stringify(trial,null,2));
    trials.push(trial);
    console.log(JSON.stringify({type:'done',id,success:trial.success,wall_ms:trial.wall_ms,model_ms:trial.model_ms,requests:trial.model_requests.length,summary:trial.summary,error:trial.error}));
    if(!trial.success) break;
  }
} finally {
  child.stdin.end();
  try{unlinkSync(join(state,'chatgpt/account.json'));}catch{}
  writeFileSync(join(root,'results.json'),JSON.stringify(trials,null,2));
}
