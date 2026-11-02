import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowClockwise, ArrowCounterClockwise, ArrowsClockwise, CaretDown, Check, CheckCircle, CloudArrowDown, Copy, Eye, EyeSlash, GitBranch, GitCommit, LockKey, MagnifyingGlass, Play, Prohibit, Rocket, Stop, TerminalWindow, Waveform, WarningCircle, X, XCircle } from '@phosphor-icons/react';
import './apps.css';

export type ControlState = {available:boolean;allowed:boolean;granted:boolean;secondFactor?:boolean};
export type GitCommitInfo = {sha:string;subject:string;author:string;time:number};
export type AppGit = {head?:string;headShort?:string;subject?:string;committedAt?:number;branch?:string|null;expectedBranch?:string;dirty?:boolean;behind?:number;ahead?:number;commits?:GitCommitInfo[];checkedAt?:number;fetched?:boolean;error?:string|null};
export type Job = {id:string;app:string;action:string;actor?:string;status:string;phase?:string|null;created?:number;started?:number|null;finished?:number|null;exitCode?:number|null;prevCommit?:string|null;newCommit?:string|null;message?:string|null};
export type ManagedApp = {id:string;name:string;hasRepo:boolean;rollbackTo?:string|null;mode:'build'|'pull';branch:string|null;hasHealthUrl?:boolean;git:AppGit|null;lastJob:Job|null;activeJob?:Job|null;containers:{running:number;total:number;unhealthy:number;items:{name:string;service:string;state:string;health:string|null}[]}};

export const ago=(seconds?:number|null)=>{if(!seconds)return '—';const d=Math.max(0,Date.now()/1000-seconds);return d<90?'just now':d<5400?`${Math.round(d/60)} min ago`:d<129600?`${Math.round(d/3600)} h ago`:`${Math.round(d/86400)} d ago`;};
export const appState=(app:ManagedApp)=>app.containers.total===0?'stopped':app.containers.unhealthy>0||app.containers.running<app.containers.total?(app.containers.running===0?'stopped':'degraded'):'running';
const stateText:Record<string,string>={running:'Running',degraded:'Degraded',stopped:'Stopped'};
export type Action='start'|'stop'|'restart'|'check'|'update'|'rollback';
export const actionLabel:Record<string,string>={start:'Start',stop:'Stop',restart:'Restart',check:'Check now',update:'Update',rollback:'Roll back',recover:'Recover'};
export const busyApp=(app:ManagedApp)=>!!app.activeJob;
async function postAction(app:string,action:Action,password?:string,code?:string){const r=await fetch(`/api/apps/${app}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(password?{password,...(code?{code}:{})}:{})});const d=await r.json().catch(()=>({}));return {ok:r.ok,status:r.status,data:d as {job?:Job;error?:string;stepUp?:boolean}};}

export function useApps(enabled:boolean){
  const [apps,setApps]=useState<ManagedApp[]|null>(null),[control,setControl]=useState<ControlState|null>(null),[error,setError]=useState('');
  const load=useCallback(async()=>{try{const r=await fetch('/api/apps',{signal:AbortSignal.timeout(9000)});const d=await r.json();if(!r.ok)throw new Error(d.error||'The app runner is not reachable.');setApps(d.apps);setControl(d.control);setError('');}catch(e){setError((e as Error).message);}},[]);
  const active=!!apps?.some(app=>app.activeJob);
  useEffect(()=>{if(!enabled)return;void load();const id=window.setInterval(()=>{if(!document.hidden)void load();},active?2000:10000);return()=>window.clearInterval(id);},[enabled,load,active]);
  return {apps,control,error,reload:load};
}

export function Apps({apps,control,error,reload,request,setRequest,onJob}:{apps:ManagedApp[]|null;control:ControlState|null;error:string;reload:()=>void;request:{app:string;action:Action}|null;setRequest:(value:{app:string;action:Action}|null)=>void;onJob:(job:Job)=>void}){
  const [open,setOpen]=useState<string|null>(null),[checking,setChecking]=useState<string|null>(null),[actionError,setActionError]=useState('');
  const pending=request&&apps?apps.find(app=>app.id===request.app)||null:null;
  async function check(app:ManagedApp){setChecking(app.id);setActionError('');const result=await postAction(app.id,'check');setChecking(null);if(!result.ok)setActionError(result.data.error||'Could not start the check.');else if(result.data.job)onJob(result.data.job);reload();}
  const behind=(apps||[]).filter(app=>(app.git?.behind||0)>0).length;
  return <><section className="apps-panel"><div className="section-heading"><h2><Rocket size={19}/>Apps<span>{(apps?.length||0).toString().padStart(2,'0')}</span></h2><span className="small-label">{behind?`${behind} WITH UPDATES`:'REGISTRY + REPOSITORIES'}</span></div>
    {error&&<p className="error apps-error" role="alert"><WarningCircle size={15}/>{error}<button onClick={reload}>Retry<ArrowClockwise size={13}/></button></p>}
    {control&&!control.allowed&&<p className="apps-note"><LockKey size={13}/>Read-only here. Control is available over Tailscale, on the host, or here once an authenticator is enrolled on the host.</p>}{actionError&&<p className="error apps-error" role="alert"><WarningCircle size={15}/>{actionError}</p>}
    <div className="app-grid">{(apps||[]).map(app=>{const state=appState(app),git=app.git,count=git?.behind||0,expanded=open===app.id;return <article className={`app-card is-${state}`} key={app.id}>
      <header><span className="service-mark"><Rocket size={17}/></span><span className="service-state"><i className={`dot ${state==='running'?'':state==='degraded'?'bad':'neutral'}`}/>{stateText[state]} · {app.containers.running}/{app.containers.total}</span><span className="app-mode">{app.hasRepo?app.mode==='pull'?'CI IMAGES':'BUILD ON HOST':'NO REPO'}</span></header>
      <strong>{app.name}</strong>
      {app.hasRepo?<p className="app-commit"><GitCommit size={13}/><code>{git?.headShort||'·······'}</code><span>{git?.subject||'Waiting for the first repository check'}</span><small>{ago(git?.committedAt)}</small></p>:<p className="app-commit muted"><GitBranch size={13}/>Lifecycle only — no repository configured.</p>}
      {app.hasRepo&&<div className="app-flags">{count>0?<button className="behind-badge" aria-expanded={expanded} onClick={()=>setOpen(expanded?null:app.id)}>{count} {count===1?'commit':'commits'} behind {app.branch}<CaretDown size={11}/></button>:git?.head?<span className="current-badge">Up to date with {app.branch}</span>:null}{git?.dirty&&<span className="warn-badge">Local changes</span>}{(git?.ahead||0)>0&&<span className="warn-badge">Diverged</span>}{git?.branch!==undefined&&git?.branch!==git?.expectedBranch&&<span className="warn-badge">On {git?.branch||'detached HEAD'}</span>}{git?.error&&<span className="warn-badge" title={git.error}>Check failed</span>}</div>}
      {expanded&&git?.commits&&<ol className="commit-list">{git.commits.map(commit=><li key={commit.sha}><code>{commit.sha}</code><span>{commit.subject}</span><small>{commit.author} · {ago(commit.time)}</small></li>)}</ol>}
      {control?.allowed&&<div className="app-actions" role="group" aria-label={`${app.name} controls`}>{app.activeJob?<button className="job-chip" onClick={()=>onJob(app.activeJob!)}><i className="dot"/>{actionLabel[app.activeJob.action as Action]||app.activeJob.action} · {app.activeJob.status}{app.activeJob.phase?` · ${app.activeJob.phase}`:''}</button>:<>
        {state==='stopped'?<button onClick={()=>setRequest({app:app.id,action:'start'})}><Play size={13}/>Start</button>:<button onClick={()=>setRequest({app:app.id,action:'stop'})}><Stop size={13}/>Stop</button>}
        <button onClick={()=>setRequest({app:app.id,action:'restart'})}><ArrowsClockwise size={13}/>Restart</button>
        {app.hasRepo&&<button className={(git?.behind||0)>0?'primary-action':''} disabled={!(git?.behind)||git?.dirty||(git?.ahead||0)>0} title={git?.dirty?'The host checkout has local changes':(git?.ahead||0)>0?'The host checkout has diverged':undefined} onClick={()=>setRequest({app:app.id,action:'update'})}><CloudArrowDown size={13}/>Update</button>}
        {app.hasRepo&&app.rollbackTo&&app.rollbackTo!==git?.headShort&&<button onClick={()=>setRequest({app:app.id,action:'rollback'})}><ArrowCounterClockwise size={13}/>Roll back</button>}
        {app.hasRepo&&<button disabled={checking===app.id} onClick={()=>void check(app)}><ArrowClockwise size={13}/>{checking===app.id?'Checking…':'Check now'}</button>}</>}</div>}
      <footer>{app.lastJob?<button className="last-job" onClick={()=>onJob(app.lastJob!)}><TerminalWindow size={12}/>Last: {app.lastJob.action} · {app.lastJob.status.replace('_',' ')} · {ago(app.lastJob.finished||app.lastJob.created)}</button>:<span>No jobs yet</span>}<span>{app.hasRepo?`Checked ${ago(git?.checkedAt)}`:''}</span></footer>
    </article>;})}
    {apps&&apps.length===0&&<div className="empty"><Rocket size={28}/><h3>No apps registered.</h3><p>Add entries to the runner registry on the host.</p></div>}</div>
  </section>{request&&pending&&<ConfirmDialog app={pending} action={request.action} granted={!!control?.granted} secondFactor={!!control?.secondFactor} onClose={()=>setRequest(null)} onDone={job=>{setRequest(null);onJob(job);reload();}}/>}</>;
}

function describe(app:ManagedApp,action:Action){
  const commits=app.git?.commits||[],count=app.git?.behind||0;
  switch(action){
    case 'start':return `Starts every service of ${app.name} with docker compose up -d.`;
    case 'stop':return `Stops every container of ${app.name}. They stay stopped until you start them again.`;
    case 'restart':return `Restarts every container of ${app.name}. Expect a short interruption.`;
    case 'update':return `Fast-forwards to ${app.branch} (${count} ${count===1?'commit':'commits'}${count>commits.length?`, ${commits.length} shown`:''}), ${app.mode==='pull'?'pulls the CI-built images':'builds on the host'}, restarts and checks health. A failed build or health check rolls back to ${app.git?.headShort||'the current commit'} automatically.`;
    case 'rollback':return `Returns ${app.name} to ${app.rollbackTo||'the commit'} — what ran before the last successful update — and redeploys it, reusing the saved images when they are still there.`;
    default:return `Fetches ${app.branch} and compares it with the deployed commit.`;
  }
}

function ConfirmDialog({app,action,granted,secondFactor,onClose,onDone}:{app:ManagedApp;action:Action;granted:boolean;secondFactor:boolean;onClose:()=>void;onDone:(job:Job)=>void}){
  const ref=useRef<HTMLDialogElement>(null);
  const [password,setPassword]=useState(''),[code,setCode]=useState(''),[show,setShow]=useState(false),[needPassword,setNeedPassword]=useState(!granted),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{ref.current?.showModal();},[]);
  async function submit(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const result=await postAction(app.id,action,needPassword?password:undefined,needPassword&&secondFactor?code:undefined);if(result.ok&&result.data.job){onDone(result.data.job);return;}if(result.data.stepUp){setNeedPassword(true);setCode('');}setError(result.status===429?'Too many attempts. Try again in a minute.':result.data.error||'The runner refused this action.');}catch{setError('The host did not answer. Try again.');}finally{setBusy(false);}}
  const danger=action==='stop'||action==='rollback';
  return <dialog ref={ref} className="confirm-dialog" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget)onClose();}} aria-labelledby="confirm-title"><form onSubmit={submit}>
    <header><span className="mono">APP / {app.id}</span><button type="button" className="icon-button" onClick={onClose} aria-label="Cancel"><X size={19}/></button></header>
    <h2 id="confirm-title">{actionLabel[action]} {app.name}?</h2><p>{describe(app,action)}</p>
    {action==='update'&&(app.git?.commits||[]).length>0&&<ol className="commit-list">{app.git!.commits!.map(commit=><li key={commit.sha}><code>{commit.sha}</code><span>{commit.subject}</span><small>{commit.author} · {ago(commit.time)}</small></li>)}</ol>}
    {needPassword&&<><label htmlFor="control-password">Access password</label><div className="password-field"><input id="control-password" type={show?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" autoFocus required/><button type="button" className="icon-button" onClick={()=>setShow(!show)} aria-label={show?'Hide password':'Show password'}>{show?<EyeSlash size={17}/>:<Eye size={17}/>}</button></div>{secondFactor&&<><label htmlFor="control-code">Authenticator code</label><input id="control-code" className="code-field" value={code} onChange={e=>setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" placeholder="123 456" maxLength={12} required/></>}<small className="confirm-hint">Confirms control for the next 10 minutes on this device.{secondFactor?' A backup code works too.':''}</small></>}
    {error&&<p className="error" role="alert">{error}</p>}
    <footer><button type="button" onClick={onClose}>Cancel</button><button className={danger?'confirm-danger':'confirm-go'} disabled={busy||(needPassword&&(!password||(secondFactor&&!code)))}>{busy?'Sending…':actionLabel[action]}</button></footer>
  </form></dialog>;
}

const STEPS:Record<string,string[]>={update:['fetch','build','up','health','done'],rollback:['fetch','build','up','health','done'],recover:['rollback','up','health','done']};
const terminal=(job:Job)=>!['queued','running'].includes(job.status);
const statusIcon=(status:string)=>status==='succeeded'?<CheckCircle size={14}/>:status==='rolled_back'||status==='cancelled'?<ArrowCounterClockwise size={14}/>:status==='failed'?<XCircle size={14}/>:<i className="dot"/>;

export function JobDrawer({job:initial,app,canControl,secondFactor=false,motion,onClose,onChanged}:{job:Job;app:ManagedApp|null;canControl:boolean;secondFactor?:boolean;motion:boolean;onClose:()=>void;onChanged:()=>void}){
  const ref=useRef<HTMLDialogElement>(null),logRef=useRef<HTMLPreElement>(null),offset=useRef(0),epoch=useRef<number|null>(null);
  const [job,setJob]=useState<Job>(initial),[log,setLog]=useState(''),[history,setHistory]=useState<Job[]>([]),[follow,setFollow]=useState(true),[wrap,setWrap]=useState(true),[query,setQuery]=useState(''),[error,setError]=useState(''),[copied,setCopied]=useState(false),[password,setPassword]=useState(''),[code,setCode]=useState(''),[needPassword,setNeedPassword]=useState(false),[busy,setBusy]=useState(false);
  useEffect(()=>{ref.current?.showModal();},[]);
  useEffect(()=>{setJob(initial);},[initial]);
  useEffect(()=>{offset.current=0;epoch.current=null;setLog('');setError('');},[job.id]);
  useEffect(()=>{let stop=false,timer=0;
    async function tick(){try{
      const [jr,lr]=await Promise.all([fetch(`/api/jobs/${job.id}`),fetch(`/api/jobs/${job.id}/log?offset=${offset.current}${epoch.current===null?'':`&epoch=${epoch.current}`}`)]);
      const jd=await jr.json(),ld=await lr.json();if(stop)return;
      if(!jr.ok)throw new Error(jd.error||'Could not read this job.');if(!lr.ok)throw new Error(ld.error||'Could not read the job log.');
      setJob(jd.job);epoch.current=ld.epoch;if(ld.reset){offset.current=ld.offset;setLog(ld.data);}else if(ld.data){offset.current=ld.offset;setLog(value=>(value+ld.data).slice(-400000));}
      if(ld.data&&ld.data.length>=65536){timer=window.setTimeout(tick,50);return;}
      if(terminal(jd.job)){onChanged();return;}
      setError('');
    }catch(e){if(!stop)setError((e as Error).message);}if(!stop)timer=window.setTimeout(tick,1000);}
    void tick();return()=>{stop=true;window.clearTimeout(timer);};
  },[job.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{if(!app)return;fetch(`/api/apps/${app.id}/jobs`).then(r=>r.ok?r.json():{jobs:[]}).then(d=>setHistory(d.jobs||[])).catch(()=>{});},[app?.id,job.status]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{if(follow&&logRef.current)logRef.current.scrollTo({top:logRef.current.scrollHeight,behavior:motion?'smooth':'auto'});},[log,follow,motion]);
  async function cancel(e?:React.FormEvent){e?.preventDefault();setBusy(true);setError('');try{const r=await fetch(`/api/jobs/${job.id}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(needPassword?{password,...(secondFactor?{code}:{})}:{})});const d=await r.json();if(!r.ok){if(d.stepUp){setNeedPassword(true);setCode('');}throw new Error(d.error||'Could not cancel this job.');}setNeedPassword(false);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  const lines=log.split('\n').filter(line=>line.toLowerCase().includes(query.toLowerCase()));
  const steps=STEPS[job.action],current=job.status==='rolled_back'?'rolled back':job.phase||'',index=steps?steps.indexOf(current==='pull'?'build':current):-1;
  return <dialog ref={ref} className="drawer job-drawer" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget)onClose();}} aria-labelledby="job-title"><div className="drawer-inner">
    <header><span className="mono">JOB / {job.id.slice(0,12)}</span><button className="icon-button" onClick={onClose} aria-label="Close job"><X size={22}/></button></header>
    <TerminalWindow size={36} className="lime" weight="duotone"/><h2 id="job-title">{actionLabel[job.action as Action]||job.action} {app?.name||job.app}</h2>
    <p className={`job-status is-${job.status}`} role="status">{statusIcon(job.status)}<strong>{job.status.replace('_',' ')}</strong>{job.message&&<span>{job.message}</span>}</p>
    <dl className="job-facts"><dt>Started</dt><dd>{job.started?new Date(job.started*1000).toLocaleString('en-GB'):'Waiting in queue'}</dd><dt>By</dt><dd>{job.actor||'—'}</dd>{job.prevCommit&&<><dt>Commits</dt><dd><code>{job.prevCommit.slice(0,7)}</code> → <code>{(job.newCommit||'…').slice(0,7)}</code></dd></>}{job.finished&&job.started&&<><dt>Duration</dt><dd>{Math.max(0,Math.round(job.finished-job.started))} s</dd></>}</dl>
    {steps&&<ol className={`job-phases ${current==='rollback'||current==='rolled back'?'is-rollback':''}`} style={{'--progress':current==='rollback'||current==='rolled back'||(terminal(job)&&job.status==='succeeded')?1:Math.max(0,index)/(steps.length-1)} as React.CSSProperties} aria-label="Deployment phases">{steps.map((step,i)=><li key={step} className={current==='rollback'||current==='rolled back'?(i<steps.length-1?'skipped':'failed'):i<index||(i===index&&terminal(job)&&job.status==='succeeded')?'done':i===index?'active':''}>{step==='build'&&app?.mode==='pull'?'pull':step}</li>)}{(current==='rollback'||current==='rolled back')&&<li className="active rollback">{current}</li>}</ol>}
    {!terminal(job)&&canControl&&<form className="job-cancel" onSubmit={cancel}>{needPassword&&<input type="password" aria-label="Access password" placeholder="Access password" value={password} onChange={e=>setPassword(e.target.value)} required/>}{needPassword&&secondFactor&&<input aria-label="Authenticator code" placeholder="Code" inputMode="numeric" autoComplete="one-time-code" maxLength={12} value={code} onChange={e=>setCode(e.target.value)} required/>}<button disabled={busy}><Prohibit size={14}/>{busy?'Cancelling…':'Cancel job'}</button></form>}
    {error&&<p className="error" role="alert">{error}</p>}
    <div className="log-header"><span>Job log</span><button onClick={async()=>{try{await navigator.clipboard.writeText(log);setCopied(true);setTimeout(()=>setCopied(false),2000);}catch{setError('Clipboard access is unavailable in this browser.');}}}>{copied?<Check/>:<Copy/>}{copied?'Copied':'Copy'}</button></div>
    <div className="log-tools"><div className="search-field"><MagnifyingGlass size={14}/><input aria-label="Filter job log" placeholder="Filter output…" value={query} onChange={e=>setQuery(e.target.value)}/>{query&&<button aria-label="Clear log filter" onClick={()=>setQuery('')}><X size={12}/></button>}</div><button className={follow?'active':''} aria-pressed={follow} onClick={()=>setFollow(!follow)}><Waveform size={14}/>Follow</button><button className={wrap?'active':''} aria-pressed={wrap} onClick={()=>setWrap(!wrap)}>Wrap</button></div>
    <div className="log-meta"><span>{query?`${lines.filter(Boolean).length} matching lines`:'GIT + COMPOSE OUTPUT'}</span><span>{terminal(job)?'COMPLETE':'LIVE / 1s'}</span></div>
    <pre ref={logRef} className={`logs job-log ${wrap?'':'no-wrap'} ${terminal(job)?'':'is-live'}`} aria-label="Job log output" aria-live="off">{log?lines.map((line,i)=><span key={i} className={line.startsWith('# ')?'log-step':/\b(error|fatal|failed)\b/i.test(line)?'log-error':/\bwarn(ing)?\b/i.test(line)?'log-warning':undefined}>{line}{'\n'}</span>):terminal(job)?'This job wrote no output.':'Waiting for output…'}</pre>
    {history.length>1&&<section className="job-history"><h3>Recent jobs</h3>{history.map(item=><button key={item.id} className={item.id===job.id?'active':''} onClick={()=>setJob(item)}><span className={`job-status is-${item.status}`}>{statusIcon(item.status)}</span><strong>{actionLabel[item.action as Action]||item.action}</strong><small>{item.status.replace('_',' ')} · {ago(item.finished||item.created)}</small></button>)}</section>}
  </div></dialog>;
}
