import { useEffect, useRef, useState } from 'react';
import { ArrowClockwise, Check, CheckCircle, WarningCircle, ChartLine, Bell } from '@phosphor-icons/react';
import { Chart, type Point } from './charts';
import './history.css';

export type Alert = {id:number;key:string;severity:string;title:string;message:string;opened:number;resolved:number|null;acknowledged:number|null};
const date = (value:number) => new Date(value*1000).toLocaleString('en-GB', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});

export function AlertJournal({alerts,onUpdate,canAcknowledge}:{alerts:Alert[];onUpdate:()=>void;canAcknowledge:boolean}) {
  const [filter,setFilter] = useState('all');
  const [error,setError] = useState('');
  const [busy,setBusy] = useState<number|null>(null);
  const [optimistic,setOptimistic] = useState<Record<number,number>>({});
  useEffect(()=>setOptimistic(current=>Object.fromEntries(
    Object.entries(current).filter(([id])=>!alerts.some(a=>a.id===Number(id)&&a.acknowledged!==null))
  )),[alerts]);
  const active = alerts.filter(a=>a.resolved===null);
  const visible = alerts.filter(a=>filter==='all'||(filter==='active'?a.resolved===null:a.resolved!==null));
  async function acknowledge(id:number) {
    setBusy(id);setError('');setOptimistic(current=>({...current,[id]:Date.now()/1000}));
    try {const response=await fetch(`/api/alerts/${id}/ack`,{method:'POST'});if(!response.ok)throw new Error('Could not acknowledge this alert. Try again.');onUpdate();}
    catch(e){setOptimistic(current=>{const next={...current};delete next[id];return next;});setError((e as Error).message);}finally{setBusy(null);}
  }
  return <section className="alert-journal" aria-label="Alert history">
    <div className="section-heading"><h2><Bell size={18}/>Alert history<span>{active.length} active</span></h2><span className="small-label">SAVED ON THIS HOST</span></div>
    <div className="journal-tools"><div className="filter-group">{['all','active','resolved'].map(f=><button key={f} className={filter===f?'active':''} aria-pressed={filter===f} onClick={()=>setFilter(f)}>{f[0].toUpperCase()+f.slice(1)}</button>)}</div><span>Last 7 days · up to 250 incidents</span></div>
    {error&&<p role="alert" className="error">{error}</p>}
    <div className="incident-list">{visible.map(a=>{const acknowledged=a.acknowledged??optimistic[a.id]??null;return <article key={a.id} className={`incident ${a.resolved===null?'is-active':''}`}>
      <div className="incident-icon">{a.resolved!==null?<CheckCircle size={23}/>:<WarningCircle size={23}/>}</div>
      <div className="incident-body"><div className="incident-heading"><strong>{a.title}</strong><span className={`incident-state ${a.resolved===null?'red':''}`}>{a.resolved!==null?'Recovered':acknowledged?'Acknowledged':a.severity}</span></div><p>{a.message}</p><div className="incident-times"><time dateTime={new Date(a.opened*1000).toISOString()}>Opened {date(a.opened)}</time>{a.resolved!==null&&<time>Recovered {date(a.resolved)}</time>}{acknowledged!==null&&<span><Check size={12}/> Seen {date(acknowledged)}</span>}</div></div>
      {a.resolved===null&&!acknowledged&&canAcknowledge&&<button className="acknowledge" disabled={busy===a.id} onClick={()=>void acknowledge(a.id)} aria-label={`Acknowledge ${a.title}`}><Check size={15}/>{busy===a.id?'Saving…':'Acknowledge'}</button>}
    </article>})}</div>
    {visible.length===0&&<div className="journal-empty"><CheckCircle size={30}/><h3>{filter==='resolved'?'No recoveries recorded yet.':filter==='active'?'No active alerts.':'Nothing to report yet.'}</h3><p>Incidents appear after a sustained problem. History begins when monitoring starts.</p></div>}
    <div className="events-footer">ACKNOWLEDGING MARKS AN ALERT AS SEEN. RECOVERY IS DETECTED AUTOMATICALLY.</div>
  </section>;
}

export function HistoryExplorer({timestamp,paused}:{timestamp:number;paused:boolean}) {
  const [range,setRange]=useState('24h');
  const [result,setResult]=useState<{points:Point[];step:number}|null>(null);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  const frozen=useRef(timestamp);
  if(!paused)frozen.current=timestamp;
  const tick=Math.floor(frozen.current/30);
  useEffect(()=>{
    const controller=new AbortController();setBusy(true);setError('');
    fetch(`/api/history?range=${range}`,{signal:controller.signal}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||'History is unavailable.');return d;}).then(setResult).catch(e=>{if(e.name!=='AbortError')setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setBusy(false);});
    return()=>controller.abort();
  },[range,tick,revision]);
  const points=result?.points||[];
  return <section className="history-explorer" aria-label="Persistent telemetry history">
    <div className="section-heading"><h2><ChartLine size={19}/>Telemetry archive</h2><div className="history-actions"><div className="range-control" aria-label="Archive range">{[['1h','1 hour'],['24h','24 hours'],['7d','7 days']].map(([id,label])=><button key={id} aria-pressed={range===id} className={range===id?'active':''} onClick={()=>{setResult(null);setRange(id);}}>{label}</button>)}</div><button className="icon-button" disabled={busy} aria-label="Refresh history" onClick={()=>setRevision(v=>v+1)}><ArrowClockwise className={busy?'spin':''}/></button></div></div>
    <div className="history-coverage" role="status">{error?<span className="red">{error}</span>:busy&&!result?'Reading saved telemetry…':points.length?`${date(points[0].time)} → ${date(points[points.length-1].time)} · ${result!.step/60} minute averages`:'History starts with the first sample. No earlier data is available.'}<span>7 DAY RETENTION</span></div>
    <div className="archive-charts">{([{field:'cpu',label:'CPU usage',unit:'%',max:100},{field:'memory',label:'Memory usage',unit:'%',max:100},{field:'temperature',label:'Temperature',unit:'°C',max:100},{field:'rx',label:'Download',unit:'B/s'},{field:'tx',label:'Upload',unit:'B/s'}] as const).map(item=>{
      const values=points.map(p=>p[item.field]).filter((v):v is number=>v!==null);
      const avg=values.length?values.reduce((a,b)=>a+b,0)/values.length:null;
      const readable=avg===null?'Unavailable':item.unit==='B/s'?`${(avg/1024).toFixed(1)} KiB/s`:`${avg.toFixed(1)} ${item.unit}`;
      return <article className="archive-chart" key={item.field}><header><h3>{item.label}</h3><span>{readable}<small> / average</small></span></header><Chart points={points} field={item.field} max={'max' in item?item.max:undefined} height={132} label={`${item.label}, ${range} history`}/></article>;
    })}</div><div className="events-footer">GAPS MEAN MISSING SAMPLES. VALUES ARE AVERAGES, SO SHORT PEAKS MAY BE SMOOTHED.</div>
  </section>;
}
