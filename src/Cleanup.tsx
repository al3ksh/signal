import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowClockwise, Broom, CheckCircle, Eye, EyeSlash, Prohibit, X } from '@phosphor-icons/react';
import { ago, type ControlState } from './Apps';
import './cleanup.css';

type Safety = 'safe'|'review'|'report-only';
type Finding = {id:string;category:string;title:string;bytes:number|null;age:number|null;safety:Safety;reason:string;action:unknown[]};
type Scan = {scannedAt:number;free:number;findings:Finding[];control:ControlState};
type Result = {id:string;title:string;applied:boolean;freed:number;log:string[];error?:string};

const size=(bytes:number|null|undefined)=>bytes==null?'—':bytes>=1e9?`${(bytes/1e9).toFixed(1)} GB`:bytes>=1e6?`${Math.round(bytes/1e6)} MB`:`${Math.max(1,Math.round(bytes/1e3))} kB`;
const total=(items:Finding[])=>items.reduce((sum,item)=>sum+(item.bytes||0),0);
const actionable=(item:Finding)=>item.safety!=='report-only'&&item.action.length>0;

/** Disk cleanup through the root pi-clean daemon. Scanning changes nothing; each item is applied by id only, after the
 * same password (and authenticator code through the public address) as app control. */
export function CleanupCard(){
  const [scan,setScan]=useState<Scan|null>(null),[error,setError]=useState(''),[hidden,setHidden]=useState(false),[busy,setBusy]=useState(false),[pending,setPending]=useState<Finding[]|null>(null);
  const load=useCallback(async(refresh=false)=>{setBusy(true);try{const r=await fetch('/api/cleanup'+(refresh?'?refresh=1':''));const d=await r.json().catch(()=>({}));if(r.status===404||r.status===401){setHidden(true);return;}if(!r.ok){setError(d.error||'The cleanup service did not answer.');return;}setScan(d);setError('');}catch{setError('The host did not answer.');}finally{setBusy(false);}},[]);
  useEffect(()=>{void load();},[load]);
  if(hidden)return null;
  const items=scan?.findings||[],safe=items.filter(i=>i.safety==='safe'&&actionable(i)),review=items.filter(i=>i.safety==='review'&&actionable(i)),report=items.filter(i=>!actionable(i));
  const control=scan?.control,canClean=!!control?.allowed;
  return <section className="pi-clean" aria-labelledby="pi-clean-title">
    <header><div><span className="small-label">DISK CLEANUP</span><h2 id="pi-clean-title"><Broom size={16}/>What can go?</h2></div>
      <div className="pi-clean-tools">{scan&&<span className="mono">scanned {ago(scan.scannedAt)}</span>}<button type="button" onClick={()=>void load(true)} disabled={busy}><ArrowClockwise size={13}/>{busy?'Scanning…':'Rescan'}</button></div></header>
    {error&&<p className="pi-clean-note error" role="alert">{error}</p>}
    {!scan&&!error&&<p className="pi-clean-note">Scanning the disk… this takes a few seconds.</p>}
    {scan&&<>
      <div className="pi-clean-totals"><span><small>Free</small>{size(scan.free)}</span><span className="is-safe"><small>Safe</small>{size(total(safe))}</span><span className="is-review"><small>Review</small>{size(total(review))}</span><span><small>Report only</small>{size(total(report))}</span>
        {canClean&&safe.length>0&&<button type="button" className="pi-clean-go" onClick={()=>setPending(safe)}><Broom size={14}/>Clean safe items ({size(total(safe))})</button>}</div>
      {!canClean&&<p className="pi-clean-note">Read-only here. Cleaning is available over Tailscale, on the host, or here once an authenticator is enrolled on the host.</p>}
      {safe.length+review.length===0?<p className="pi-clean-note"><CheckCircle size={13}/>Nothing to clean right now.</p>:
        <ul>{[...safe,...review].map(item=><li key={item.id} className={`is-${item.safety}`}><span className="pi-clean-tag">{item.safety}</span><div><strong>{item.title}</strong><small>{item.reason}{item.age?` · ${Math.round(item.age/86400)} d old`:''}</small></div><span className="mono">{size(item.bytes)}</span>{canClean&&<button type="button" onClick={()=>setPending([item])}>Clean</button>}</li>)}</ul>}
      {report.length>0&&<details><summary>Report only — {report.length} item{report.length===1?'':'s'}, decide by hand ({size(total(report))})</summary><ul>{report.map(item=><li key={item.id} className="is-report"><Prohibit size={13}/><div><strong>{item.title}</strong><small>{item.reason}</small></div><span className="mono">{size(item.bytes)}</span></li>)}</ul></details>}
    </>}
    {pending&&control&&<CleanDialog items={pending} control={control} onClose={()=>setPending(null)} onDone={()=>void load()}/>}
  </section>;
}

function CleanDialog({items,control,onClose,onDone}:{items:Finding[];control:ControlState;onClose:()=>void;onDone:()=>void}){
  const ref=useRef<HTMLDialogElement>(null),secondFactor=!!control.secondFactor;
  const [password,setPassword]=useState(''),[code,setCode]=useState(''),[show,setShow]=useState(false),[needPassword,setNeedPassword]=useState(!control.granted),[busy,setBusy]=useState(false),[error,setError]=useState(''),[results,setResults]=useState<Result[]>([]);
  useEffect(()=>{ref.current?.showModal();},[]);
  const done=results.length===items.length;
  async function submit(e:React.FormEvent){
    e.preventDefault();if(done){onClose();return;}
    setBusy(true);setError('');
    let step=needPassword;
    try{
      // One request per item; the first one carries the step-up and the 10-minute grant covers the rest.
      for(const item of items.slice(results.length)){
        const r=await fetch('/api/cleanup/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:item.id,...(step?{password,...(secondFactor?{code}:{})}:{})})});
        const d=await r.json().catch(()=>({}));
        if(d.stepUp){setNeedPassword(true);setCode('');setError(d.error||'Confirm with your access password.');return;}
        if(r.status===429){setError(d.error||'Too many attempts. Try again in a minute.');return;}
        step=false;setNeedPassword(false);
        setResults(prev=>[...prev,r.ok?{id:item.id,title:item.title,applied:!!d.applied,freed:d.freed||0,log:d.log||[]}:{id:item.id,title:item.title,applied:false,freed:0,log:[],error:d.error||'Refused.'}]);
      }
      onDone();
    }catch{setError('The host did not answer. The item may still be running; rescan in a minute.');}finally{setBusy(false);}
  }
  const freed=results.reduce((sum,r)=>sum+r.freed,0);
  return <dialog ref={ref} className="confirm-dialog clean-dialog" onCancel={e=>{if(busy)e.preventDefault();else onClose();}} aria-labelledby="clean-title"><form onSubmit={submit}>
    <header><span className="mono">HOST / pi-clean</span><button type="button" className="icon-button" onClick={onClose} disabled={busy} aria-label="Close"><X size={19}/></button></header>
    <h2 id="clean-title">{done?`Freed ${size(freed)}`:`Clean ${items.length===1?items[0].title:`${items.length} items`}?`}</h2>
    {!done&&<p>{items.length===1?items[0].reason:'Each item is re-checked on the host right before it runs; anything that no longer qualifies is skipped.'} Runs as root on the Pi; every step is logged to /var/log/pi-clean.log.</p>}
    <ol className="clean-steps">{items.map((item,i)=>{const r=results[i];return <li key={item.id} className={r?(r.error||!r.applied?'is-skipped':'is-done'):busy&&i===results.length?'is-running':''}>
      <div><strong>{item.title}</strong><span className="mono">{r?r.error||(r.applied?`${size(r.freed)} freed`:'skipped'):size(item.bytes)}</span></div>
      {r&&r.log.length>0&&<pre>{r.log.join('\n')}</pre>}</li>;})}</ol>
    {needPassword&&!done&&<><label htmlFor="clean-password">Access password</label><div className="password-field"><input id="clean-password" type={show?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" autoFocus required/><button type="button" className="icon-button" onClick={()=>setShow(!show)} aria-label={show?'Hide password':'Show password'}>{show?<EyeSlash size={17}/>:<Eye size={17}/>}</button></div>{secondFactor&&<><label htmlFor="clean-code">Authenticator code</label><input id="clean-code" className="code-field" value={code} onChange={e=>setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" placeholder="123 456" maxLength={12} required/></>}<small className="confirm-hint">Confirms control for the next 10 minutes on this device.{secondFactor?' A backup code works too.':''}</small></>}
    {error&&<p className="error" role="alert">{error}</p>}
    <footer>{!done&&<button type="button" onClick={onClose} disabled={busy}>Cancel</button>}<button className="confirm-go" disabled={busy||(!done&&needPassword&&(!password||(secondFactor&&!code)))}>{busy?'Cleaning…':done?'Close':'Clean'}</button></footer>
  </form></dialog>;
}
