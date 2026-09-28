// Diagnostic only: executes installed AgentsRoom code with controlled host boundaries.
// No app files, sessions, credentials or network are modified.
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const asar = fs.readFileSync(process.argv.slice(2).find(a => !a.startsWith('--')) || '/Applications/AgentsRoom.app/Contents/Resources/app.asar');
const header = JSON.parse(asar.subarray(16, 16 + asar.readUInt32LE(12)));
function read(path) {
  let e = header;
  for (const bit of path.split('/')) e = e.files[bit];
  const start = 8 + asar.readUInt32LE(4) + Number(e.offset);
  return asar.subarray(start, start + e.size).toString();
}
const html = read('dist/index.html');
const main = read('dist/' + html.match(/src="\.\/([^" ]+\.js)"/)[1]);
const file = main.match(/\.\/useUiZoom-[\w-]+\.js/)[0].slice(2);
const bundle = read('dist/assets/' + file);
function extract(start, end) {
  const a = bundle.indexOf(start), b = bundle.indexOf(end, a);
  if (a < 0 || b < 0) throw Error('Unsupported app build: extraction anchors changed');
  return bundle.slice(a, b);
}
const candidate = process.argv.includes('--candidate');
let source = extract('function aWt(e){', 'function Vee(e){');
let transfer = extract('async function vc(', 'async function h0(');
if (candidate) {
  // Diagnostic edits in memory only; upstream must implement the full protocol.
  source = source.replace('function aWt(e){', 'async function aWt(e){')
    .replace('window.electronAPI.deleteFile(a).catch(()=>{})', 'await window.electronAPI.deleteFile(a)')
    .replace('if(x&&x.trim().length>20)', 'if(d&&f==="done"&&x&&x.trim().length>20)');
  transfer = transfer.replace('}`,It)}catch{}qee.set', '}`,It)}catch(error){return {failure:"write-failed"}}qee.set');
}
console.log(candidate ? 'MODE: in-memory candidate (not installed)' : 'MODE: installed code');
console.log('Installed bundle SHA256:', crypto.createHash('sha256').update(bundle).digest('hex'));
async function check(label, { status, content, pendingDelete, complete }, expectPending) {
  let tick, settled = false, result;
  const context = { _w: new Map(), rWt: 120000, XSe: 1500,
    nEe: () => 'summary.md', Uc: () => true, Zx: () => {}, AXe: () => '', jw: () => {},
    Ae: { getState: () => ({ getSession: () => ({status}) }) },
    window: {setInterval: fn => (tick = fn, 1), clearInterval: () => {}, electronAPI: {
      deleteFile: () => pendingDelete ? new Promise(() => {}) : Promise.resolve(),
      readFile: async () => content,
    }},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const starting = context.aWt({agentId:'fixture', projectId:'fixture', ptyId:'fixture', projectPath:'/fixture'});
  Promise.resolve(starting).then(job => job.promise).then(value => { settled = true; result = value; });
  for (let i=0;i<5;i++) await Promise.resolve();
  if (tick) await tick();
  if (complete) { status = 'done'; content = 'Completed task, verified results and next action.'; if (tick) await tick(); }
  for (let i=0;i<5;i++) await Promise.resolve();
  const pass = expectPending ? !settled : settled && result.summary === content;
  console.log(`${pass ? 'PASS' : 'FAIL'} ${label}: settled=${settled}, status=${status}, summaryChars=${result?.summary?.length || 0}`);
  return pass;
}
async function checkWriteFailure(fail) {
  let killed = 0, writes = 0;
  const queue = new Map();
  const context = {e:'fixture',t:'project',n:'/fixture',N:{current:{writeln(){},clear(){}}},
    O:{current:'old-pty'},K:{current:null},Y:{current:null},
    so:()=>({provider:'codex',model:'fixture'}),mu:{getState:()=>({close(){}})},
    Ae:{getState:()=>({getSession:()=>({launchedProvider:'codex',launchedModel:'fixture'}),setStatus(){}})},
    _i:{getState:()=>({getAgentFiles:()=>[]})},jXe:()=> 'transcript.txt',sge:x=>JSON.stringify(x),qee:queue,
    Ds:()=>null,WFe:()=>false,Bt:{getState:()=>({updateAgent(){}})},LI:new Set(),hs:new Map(),gm(){},
    ps:()=>({displayName:'Codex'}),f:x=>x,setTimeout(){},
    window:{electronAPI:{writeFile:async()=>{writes++;if(fail)throw Object.assign(Error('Permission denied'),{code:'EACCES'})},killPTY:async()=>{killed++}}},
  };
  vm.createContext(context);vm.runInContext(transfer,context);
  await context.vc('codex','fixture',{transcript:'Captured transcript fixture'});
  const pass=fail ? killed===0&&queue.size===0 : killed===1&&queue.size===1;
  console.log(`${pass?'PASS':'FAIL'} transcript ${fail?'permission denial preserves producer':'success control'}: writes=${writes}, killed=${killed}, queued=${queue.size}`);
  return pass;
}
(async () => {
  const results = [];
  results.push(await check('completed summary control', {status:'thinking', content:'', complete:true}, false));
  results.push(await check('partial summary must not transfer while writer is thinking', {status:'thinking',content:'Task: unfinished checkpoint prefix only'}, true));
  results.push(await check('previous summary must not transfer before deletion finishes', {status:'thinking',content:'Previous task summary from an earlier transfer.',pendingDelete:true}, true));
  results.push(await checkWriteFailure(false));
  results.push(await checkWriteFailure(true));
  process.exitCode = results.every(Boolean) ? 0 : 1;
})().catch(e => { console.error(e.message); process.exitCode = 2; });
