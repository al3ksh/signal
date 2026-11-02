import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowClockwise, ArrowsClockwise, CaretDown, Eye, EyeSlash, GitBranch, GitCommit, LockKey, Play, Rocket, Stop, WarningCircle, X } from '@phosphor-icons/react';
import './apps.css';

export type ControlState = {available:boolean;allowed:boolean;granted:boolean};
export type GitCommitInfo = {sha:string;subject:string;author:string;time:number};
export type AppGit = {head?:string;headShort?:string;subject?:string;committedAt?:number;branch?:string|null;expectedBranch?:string;dirty?:boolean;behind?:number;ahead?:number;commits?:GitCommitInfo[];checkedAt?:number;fetched?:boolean;error?:string|null};
export type Job = {id:string;app:string;action:string;actor?:string;status:string;phase?:string|null;created?:number;started?:number|null;finished?:number|null;exitCode?:number|null;prevCommit?:string|null;newCommit?:string|null;message?:string|null};
export type ManagedApp = {id:string;name:string;hasRepo:boolean;mode:'build'|'pull';branch:string|null;hasHealthUrl?:boolean;git:AppGit|null;lastJob:Job|null;activeJob?:Job|null;containers:{running:number;total:number;unhealthy:number;items:{name:string;service:string;state:string;health:string|null}[]}};

export const ago=(seconds?:number|null)=>{if(!seconds)return '—';const d=Math.max(0,Date.now()/1000-seconds);return d<90?'just now':d<5400?`${Math.round(d/60)} min ago`:d<129600?`${Math.round(d/3600)} h ago`:`${Math.round(d/86400)} d ago`;};
export const appState=(app:ManagedApp)=>app.containers.total===0?'stopped':app.containers.unhealthy>0||app.containers.running<app.containers.total?(app.containers.running===0?'stopped':'degraded'):'running';
const stateText:Record<string,string>={running:'Running',degraded:'Degraded',stopped:'Stopped'};
export type Action='start'|'stop'|'restart'|'check'|'update'|'rollback';
export const actionLabel:Record<Action,string>={start:'Start',stop:'Stop',restart:'Restart',check:'Check now',update:'Update',rollback:'Roll back'};
export const busyApp=(app:ManagedApp)=>!!app.activeJob;
async function postAction(app:string,action:Action,password?:string){const r=await fetch(`/api/apps/${app}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(password?{password}:{})});const d=await r.json().catch(()=>({}));return {ok:r.ok,status:r.status,data:d as {job?:Job;error?:string;stepUp?:boolean}};}

export function useApps(enabled:boolean){
  const [apps,setApps]=useState<ManagedApp[]|null>(null),[control,setControl]=useState<ControlState|null>(null),[error,setError]=useState('');
  const load=useCallback(async()=>{try{const r=await fetch('/api/apps',{signal:AbortSignal.timeout(9000)});const d=await r.json();if(!r.ok)throw new Error(d.error||'The app runner is not reachable.');setApps(d.apps);setControl(d.control);setError('');}catch(e){setError((e as Error).message);}},[]);
  useEffect(()=>{if(!enabled)return;void load();const id=window.setInterval(()=>{if(!document.hidden)void load();},10000);return()=>window.clearInterval(id);},[enabled,load]);
  return {apps,control,error,reload:load};
}

export function Apps({apps,control,error,reload,request,setRequest,onJob}:{apps:ManagedApp[]|null;control:ControlState|null;error:string;reload:()=>void;request:{app:string;action:Action}|null;setRequest:(value:{app:string;action:Action}|null)=>void;onJob:(job:Job)=>void}){
  const [open,setOpen]=useState<string|null>(null),[checking,setChecking]=useState<string|null>(null),[actionError,setActionError]=useState('');
  const pending=request&&apps?apps.find(app=>app.id===request.app)||null:null;
  async function check(app:ManagedApp){setChecking(app.id);setActionError('');const result=await postAction(app.id,'check');setChecking(null);if(!result.ok)setActionError(result.data.error||'Could not start the check.');else if(result.data.job)onJob(result.data.job);reload();}
  const behind=(apps||[]).filter(app=>(app.git?.behind||0)>0).length;
  return <><section className="apps-panel"><div className="section-heading"><h2><Rocket size={19}/>Apps<span>{(apps?.length||0).toString().padStart(2,'0')}</span></h2><span className="small-label">{behind?`${behind} WITH UPDATES`:'REGISTRY + REPOSITORIES'}</span></div>
    {error&&<p className="error apps-error" role="alert"><WarningCircle size={15}/>{error}<button onClick={reload}>Retry<ArrowClockwise size={13}/></button></p>}
    {control&&!control.allowed&&<p className="apps-note"><LockKey size={13}/>Read-only here. Control is available over Tailscale or on the host.</p>}{actionError&&<p className="error apps-error" role="alert"><WarningCircle size={15}/>{actionError}</p>}
    <div className="app-grid">{(apps||[]).map(app=>{const state=appState(app),git=app.git,count=git?.behind||0,expanded=open===app.id;return <article className={`app-card is-${state}`} key={app.id}>
      <header><span className="service-mark"><Rocket size={17}/></span><span className="service-state"><i className={`dot ${state==='running'?'':state==='degraded'?'bad':'neutral'}`}/>{stateText[state]} · {app.containers.running}/{app.containers.total}</span><span className="app-mode">{app.hasRepo?app.mode==='pull'?'CI IMAGES':'BUILD ON HOST':'NO REPO'}</span></header>
      <strong>{app.name}</strong>
      {app.hasRepo?<p className="app-commit"><GitCommit size={13}/><code>{git?.headShort||'·······'}</code><span>{git?.subject||'Waiting for the first repository check'}</span><small>{ago(git?.committedAt)}</small></p>:<p className="app-commit muted"><GitBranch size={13}/>Lifecycle only — no repository configured.</p>}
      {app.hasRepo&&<div className="app-flags">{count>0?<button className="behind-badge" aria-expanded={expanded} onClick={()=>setOpen(expanded?null:app.id)}>{count} {count===1?'commit':'commits'} behind {app.branch}<CaretDown size={11}/></button>:git?.head?<span className="current-badge">Up to date with {app.branch}</span>:null}{git?.dirty&&<span className="warn-badge">Local changes</span>}{(git?.ahead||0)>0&&<span className="warn-badge">Diverged</span>}{git?.branch!==undefined&&git?.branch!==git?.expectedBranch&&<span className="warn-badge">On {git?.branch||'detached HEAD'}</span>}{git?.error&&<span className="warn-badge" title={git.error}>Check failed</span>}</div>}
      {expanded&&git?.commits&&<ol className="commit-list">{git.commits.map(commit=><li key={commit.sha}><code>{commit.sha}</code><span>{commit.subject}</span><small>{commit.author} · {ago(commit.time)}</small></li>)}</ol>}
      {control?.allowed&&<div className="app-actions" role="group" aria-label={`${app.name} controls`}>{app.activeJob?<button className="job-chip" onClick={()=>onJob(app.activeJob!)}><i className="dot"/>{app.activeJob.action} {app.activeJob.status}{app.activeJob.phase?` · ${app.activeJob.phase}`:''}</button>:<>
        {state==='stopped'?<button onClick={()=>setRequest({app:app.id,action:'start'})}><Play size={13}/>Start</button>:<button onClick={()=>setRequest({app:app.id,action:'stop'})}><Stop size={13}/>Stop</button>}
        <button onClick={()=>setRequest({app:app.id,action:'restart'})}><ArrowsClockwise size={13}/>Restart</button>
        {app.hasRepo&&<button disabled={checking===app.id} onClick={()=>void check(app)}><ArrowClockwise size={13}/>{checking===app.id?'Checking…':'Check now'}</button>}</>}</div>}
      <footer><span>{app.lastJob?`Last: ${app.lastJob.action} · ${app.lastJob.status.replace('_',' ')} · ${ago(app.lastJob.finished||app.lastJob.created)}`:'No jobs yet'}</span><span>{app.hasRepo?`Checked ${ago(git?.checkedAt)}`:''}</span></footer>
    </article>;})}
    {apps&&apps.length===0&&<div className="empty"><Rocket size={28}/><h3>No apps registered.</h3><p>Add entries to the runner registry on the host.</p></div>}</div>
  </section>{request&&pending&&<ConfirmDialog app={pending} action={request.action} granted={!!control?.granted} onClose={()=>setRequest(null)} onDone={job=>{setRequest(null);onJob(job);reload();}}/>}</>;
}

function describe(app:ManagedApp,action:Action){
  const commits=app.git?.commits||[],count=app.git?.behind||0;
  switch(action){
    case 'start':return `Starts every service of ${app.name} with docker compose up -d.`;
    case 'stop':return `Stops every container of ${app.name}. They stay stopped until you start them again.`;
    case 'restart':return `Restarts every container of ${app.name}. Expect a short interruption.`;
    case 'update':return `Fast-forwards to ${app.branch} (${count} ${count===1?'commit':'commits'}${count>commits.length?`, ${commits.length} shown`:''}), ${app.mode==='pull'?'pulls the CI-built images':'builds on the host'}, restarts and checks health. A failed build or health check rolls back to ${app.git?.headShort||'the current commit'} automatically.`;
    case 'rollback':return `Returns ${app.name} to the commit that was running before the last successful update and redeploys it.`;
    default:return `Fetches ${app.branch} and compares it with the deployed commit.`;
  }
}

function ConfirmDialog({app,action,granted,onClose,onDone}:{app:ManagedApp;action:Action;granted:boolean;onClose:()=>void;onDone:(job:Job)=>void}){
  const ref=useRef<HTMLDialogElement>(null);
  const [password,setPassword]=useState(''),[show,setShow]=useState(false),[needPassword,setNeedPassword]=useState(!granted),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{ref.current?.showModal();},[]);
  async function submit(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');try{const result=await postAction(app.id,action,needPassword?password:undefined);if(result.ok&&result.data.job){onDone(result.data.job);return;}if(result.data.stepUp)setNeedPassword(true);setError(result.status===429?'Too many attempts. Try again in a minute.':result.data.error||'The runner refused this action.');}catch{setError('The host did not answer. Try again.');}finally{setBusy(false);}}
  const danger=action==='stop'||action==='rollback';
  return <dialog ref={ref} className="confirm-dialog" onCancel={onClose} onClick={e=>{if(e.target===e.currentTarget)onClose();}} aria-labelledby="confirm-title"><form onSubmit={submit}>
    <header><span className="mono">APP / {app.id}</span><button type="button" className="icon-button" onClick={onClose} aria-label="Cancel"><X size={19}/></button></header>
    <h2 id="confirm-title">{actionLabel[action]} {app.name}?</h2><p>{describe(app,action)}</p>
    {action==='update'&&(app.git?.commits||[]).length>0&&<ol className="commit-list">{app.git!.commits!.map(commit=><li key={commit.sha}><code>{commit.sha}</code><span>{commit.subject}</span><small>{commit.author} · {ago(commit.time)}</small></li>)}</ol>}
    {needPassword&&<><label htmlFor="control-password">Access password</label><div className="password-field"><input id="control-password" type={show?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" autoFocus required/><button type="button" className="icon-button" onClick={()=>setShow(!show)} aria-label={show?'Hide password':'Show password'}>{show?<EyeSlash size={17}/>:<Eye size={17}/>}</button></div><small className="confirm-hint">Confirms control for the next 10 minutes on this device.</small></>}
    {error&&<p className="error" role="alert">{error}</p>}
    <footer><button type="button" onClick={onClose}>Cancel</button><button className={danger?'confirm-danger':'confirm-go'} disabled={busy||(needPassword&&!password)}>{busy?'Sending…':actionLabel[action]}</button></footer>
  </form></dialog>;
}
