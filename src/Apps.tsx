import { useCallback, useEffect, useState } from 'react';
import { ArrowClockwise, CaretDown, GitBranch, GitCommit, Rocket, WarningCircle } from '@phosphor-icons/react';
import './apps.css';

export type ControlState = {available:boolean;allowed:boolean;granted:boolean};
export type GitCommitInfo = {sha:string;subject:string;author:string;time:number};
export type AppGit = {head?:string;headShort?:string;subject?:string;committedAt?:number;branch?:string|null;expectedBranch?:string;dirty?:boolean;behind?:number;ahead?:number;commits?:GitCommitInfo[];checkedAt?:number;fetched?:boolean;error?:string|null};
export type Job = {id:string;app:string;action:string;actor?:string;status:string;phase?:string|null;created?:number;started?:number|null;finished?:number|null;exitCode?:number|null;prevCommit?:string|null;newCommit?:string|null;message?:string|null};
export type ManagedApp = {id:string;name:string;hasRepo:boolean;mode:'build'|'pull';branch:string|null;hasHealthUrl?:boolean;git:AppGit|null;lastJob:Job|null;activeJob?:Job|null;containers:{running:number;total:number;unhealthy:number;items:{name:string;service:string;state:string;health:string|null}[]}};

export const ago=(seconds?:number|null)=>{if(!seconds)return '—';const d=Math.max(0,Date.now()/1000-seconds);return d<90?'just now':d<5400?`${Math.round(d/60)} min ago`:d<129600?`${Math.round(d/3600)} h ago`:`${Math.round(d/86400)} d ago`;};
export const appState=(app:ManagedApp)=>app.containers.total===0?'stopped':app.containers.unhealthy>0||app.containers.running<app.containers.total?(app.containers.running===0?'stopped':'degraded'):'running';
const stateText:Record<string,string>={running:'Running',degraded:'Degraded',stopped:'Stopped'};

export function useApps(enabled:boolean){
  const [apps,setApps]=useState<ManagedApp[]|null>(null),[control,setControl]=useState<ControlState|null>(null),[error,setError]=useState('');
  const load=useCallback(async()=>{try{const r=await fetch('/api/apps',{signal:AbortSignal.timeout(9000)});const d=await r.json();if(!r.ok)throw new Error(d.error||'The app runner is not reachable.');setApps(d.apps);setControl(d.control);setError('');}catch(e){setError((e as Error).message);}},[]);
  useEffect(()=>{if(!enabled)return;void load();const id=window.setInterval(()=>{if(!document.hidden)void load();},10000);return()=>window.clearInterval(id);},[enabled,load]);
  return {apps,control,error,reload:load};
}

export function Apps({apps,control,error,reload}:{apps:ManagedApp[]|null;control:ControlState|null;error:string;reload:()=>void}){
  const [open,setOpen]=useState<string|null>(null);
  const behind=(apps||[]).filter(app=>(app.git?.behind||0)>0).length;
  return <section className="apps-panel"><div className="section-heading"><h2><Rocket size={19}/>Apps<span>{(apps?.length||0).toString().padStart(2,'0')}</span></h2><span className="small-label">{behind?`${behind} WITH UPDATES`:'REGISTRY + REPOSITORIES'}</span></div>
    {error&&<p className="error apps-error" role="alert"><WarningCircle size={15}/>{error}<button onClick={reload}>Retry<ArrowClockwise size={13}/></button></p>}
    {control&&!control.allowed&&<p className="apps-note">Read-only here. Control is available over Tailscale or on the host.</p>}
    <div className="app-grid">{(apps||[]).map(app=>{const state=appState(app),git=app.git,count=git?.behind||0,expanded=open===app.id;return <article className={`app-card is-${state}`} key={app.id}>
      <header><span className="service-mark"><Rocket size={17}/></span><span className="service-state"><i className={`dot ${state==='running'?'':state==='degraded'?'bad':'neutral'}`}/>{stateText[state]} · {app.containers.running}/{app.containers.total}</span><span className="app-mode">{app.hasRepo?app.mode==='pull'?'CI IMAGES':'BUILD ON HOST':'NO REPO'}</span></header>
      <strong>{app.name}</strong>
      {app.hasRepo?<p className="app-commit"><GitCommit size={13}/><code>{git?.headShort||'·······'}</code><span>{git?.subject||'Waiting for the first repository check'}</span><small>{ago(git?.committedAt)}</small></p>:<p className="app-commit muted"><GitBranch size={13}/>Lifecycle only — no repository configured.</p>}
      {app.hasRepo&&<div className="app-flags">{count>0?<button className="behind-badge" aria-expanded={expanded} onClick={()=>setOpen(expanded?null:app.id)}>{count} {count===1?'commit':'commits'} behind {app.branch}<CaretDown size={11}/></button>:git?.head?<span className="current-badge">Up to date with {app.branch}</span>:null}{git?.dirty&&<span className="warn-badge">Local changes</span>}{(git?.ahead||0)>0&&<span className="warn-badge">Diverged</span>}{git?.branch!==undefined&&git?.branch!==git?.expectedBranch&&<span className="warn-badge">On {git?.branch||'detached HEAD'}</span>}{git?.error&&<span className="warn-badge" title={git.error}>Check failed</span>}</div>}
      {expanded&&git?.commits&&<ol className="commit-list">{git.commits.map(commit=><li key={commit.sha}><code>{commit.sha}</code><span>{commit.subject}</span><small>{commit.author} · {ago(commit.time)}</small></li>)}</ol>}
      <footer><span>{app.lastJob?`Last: ${app.lastJob.action} · ${app.lastJob.status.replace('_',' ')} · ${ago(app.lastJob.finished||app.lastJob.created)}`:'No jobs yet'}</span><span>{app.hasRepo?`Checked ${ago(git?.checkedAt)}`:''}</span></footer>
    </article>;})}
    {apps&&apps.length===0&&<div className="empty"><Rocket size={28}/><h3>No apps registered.</h3><p>Add entries to the runner registry on the host.</p></div>}</div>
  </section>;
}
