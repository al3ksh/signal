import { useEffect, useRef, useState, type ReactNode, type PointerEvent } from 'react';
import './login-play.css';

export function LoginPlay({children}:{children:ReactNode}) {
  const [angle,setAngle]=useState(0),[awake,setAwake]=useState(false),[locked,setLocked]=useState(false),[held,setHeld]=useState(false);
  const [pings,setPings]=useState<{id:number;x:number;y:number}[]>([]);
  const dial=useRef<HTMLDivElement>(null),drag=useRef<number|null>(null),serial=useRef(0);
  const timers=useRef(new Set<ReturnType<typeof setTimeout>>());
  const later=(fn:()=>void,ms:number)=>{const timer=setTimeout(()=>{timers.current.delete(timer);fn();},ms);timers.current.add(timer);};
  useEffect(()=>()=>timers.current.forEach(clearTimeout),[]);
  const tune=(value:number)=>{setAwake(true);setAngle(Math.max(0,Math.min(270,value)));};
  useEffect(()=>{if(!awake||Math.abs(angle-162)>7||locked)return;setLocked(true);const timer=setTimeout(()=>{setLocked(false);setAwake(false);setAngle(0);},4000);return()=>clearTimeout(timer);},[angle,awake]);
  function bearing(e:PointerEvent<HTMLDivElement>){const r=e.currentTarget.getBoundingClientRect();return Math.atan2(e.clientY-r.top-r.height/2,e.clientX-r.left-r.width/2)*180/Math.PI;}
  const noise=locked?0:Math.abs(angle-162)/162;
  const wave=Array.from({length:101},(_,i)=>`${i*3},${40+Math.sin(i*.24)*12+noise*(Math.sin(i*2.13)*11+Math.cos(i*.87)*8)}`).join(' ');
  return <div className="login-page login-play" onPointerDown={e=>{
    if((e.target as HTMLElement).closest('button,input,form,a,[role="slider"]'))return;
    const r=e.currentTarget.getBoundingClientRect(),id=++serial.current;
    setPings(p=>[...p.slice(-4),{id,x:e.clientX-r.left,y:e.clientY-r.top}]);later(()=>setPings(p=>p.filter(v=>v.id!==id)),1800);
  }}>
    <button type="button" className={`brand secret-brand ${held?'held':''}`} aria-label="SIGNAL — hold to reveal circuit" onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);setHeld(true);}} onPointerUp={()=>setHeld(false)} onPointerCancel={()=>setHeld(false)} onLostPointerCapture={()=>setHeld(false)} onKeyDown={e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();setHeld(true);}}} onKeyUp={()=>setHeld(false)} onBlur={()=>setHeld(false)}>
      <svg viewBox="0 0 80 40" aria-hidden="true"><path className="circuit-a" d="M2 22H20L28 6L40 34L49 14L56 22H78"/><path className="circuit-b" d="M2 22H20V8H60V22H78"/><path className="circuit-c" d="M2 22H12V36H68V22H78"/><circle cx="2" cy="22" r="2"/><circle cx="78" cy="22" r="2"/></svg>SIGNAL<span>v2</span>
    </button>
    <div ref={dial} className={`signal-tuner ${awake?'awake':''} ${locked?'locked':''}`} role="slider" tabIndex={0} aria-label="Tune signal frequency" aria-valuemin={0} aria-valuemax={270} aria-valuenow={Math.round(angle)} aria-valuetext={locked?'Signal locked':`${(88+angle/10).toFixed(1)} MHz`} onPointerDown={e=>{if(e.button!==0)return;e.currentTarget.setPointerCapture(e.pointerId);drag.current=bearing(e);setAwake(true);}} onPointerMove={e=>{if(drag.current===null)return;const next=bearing(e),delta=((next-drag.current+540)%360)-180;drag.current=next;if(!locked)tune(angle+delta);}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onLostPointerCapture={()=>{drag.current=null;}} onKeyDown={e=>{if(['ArrowLeft','ArrowDown','ArrowRight','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();if(!locked)tune(e.key==='Home'?0:e.key==='End'?270:angle+(['ArrowLeft','ArrowDown'].includes(e.key)?-3:3));}}}>
      <svg className="tuner-dial" viewBox="0 0 600 600" aria-hidden="true"><circle cx="300" cy="300" r="285"/><circle cx="300" cy="300" r="222" strokeDasharray="1 13"/><circle cx="300" cy="300" r="126"/>{Array.from({length:60},(_,i)=><path key={i} transform={`rotate(${i*6} 300 300)`} d={`M300 15V${i%5===0?32:22}`}/>)}<g transform={`rotate(${angle} 300 300)`}><path className="dial-needle" d="M300 9V43"/><circle className="dial-dot" cx="300" cy="78" r="4"/></g></svg>
      <div className="tuner-core"><svg viewBox="0 0 300 80" aria-hidden="true"><polyline points={wave}/></svg><span className="tuner-frequency">{(88+angle/10).toFixed(1)} MHz</span><span className="tuner-status" role="status">{locked?'SIGNAL LOCKED':awake?'SEARCHING…':''}</span><small>{locked?'someone is listening':''}</small></div>
      {locked&&<i className="lock-echo" aria-hidden="true"/>}
    </div>
    {pings.map(p=><div key={p.id} className="signal-ping" style={{left:p.x,top:p.y}} aria-hidden="true"><i/><i/></div>)}
    {pings.length>0&&<div className="ring-echo" key={pings[pings.length-1].id} aria-hidden="true"/>}
    {children}
  </div>;
}
