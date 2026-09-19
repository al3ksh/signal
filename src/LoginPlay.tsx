import { useCallback, useEffect, useRef, useState, type ReactNode, type PointerEvent } from 'react';
import './login-play.css';

const HOME_ANGLE=0;
function randomLockAngle(previous?:number){
  let next=108+Math.random()*134;
  if(previous!==undefined&&Math.abs(next-previous)<38)next=previous<175?Math.min(242,previous+55):Math.max(108,previous-55);
  return Math.round(next);
}

export function LoginPlay({children}:{children:ReactNode}) {
  const [angle,setAngle]=useState(HOME_ANGLE),[target,setTarget]=useState(()=>randomLockAngle()),[awake,setAwake]=useState(false),[locked,setLocked]=useState(false),[returning,setReturning]=useState(false),[held,setHeld]=useState(false);
  const [pings,setPings]=useState<{id:number;x:number;y:number}[]>([]);
  const dial=useRef<HTMLDivElement>(null),drag=useRef<number|null>(null),serial=useRef(0),angleValue=useRef(HOME_ANGLE),returnFrame=useRef<number|null>(null);
  const timers=useRef(new Set<ReturnType<typeof setTimeout>>());
  const later=(fn:()=>void,ms:number)=>{const timer=setTimeout(()=>{timers.current.delete(timer);fn();},ms);timers.current.add(timer);};
  const commitAngle=useCallback((value:number)=>{angleValue.current=value;setAngle(value);},[]);
  const stopReturn=useCallback(()=>{if(returnFrame.current!==null)cancelAnimationFrame(returnFrame.current);returnFrame.current=null;setReturning(false);},[]);
  useEffect(()=>()=>{timers.current.forEach(clearTimeout);if(returnFrame.current!==null)cancelAnimationFrame(returnFrame.current);},[]);
  const tune=(value:number)=>{
    const previous=angleValue.current,next=Math.max(HOME_ANGLE,Math.min(270,value));
    setAwake(true);
    if(!locked&&!returning&&((previous<=target&&next>=target)||(previous>=target&&next<=target)||Math.abs(next-target)<=6)){commitAngle(target);setLocked(true);return;}
    commitAngle(next);
  };
  const returnHome=useCallback(()=>{
    setLocked(false);setReturning(true);
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches||document.documentElement.dataset.motion==='reduced';
    if(reduced){commitAngle(HOME_ANGLE);setReturning(false);setAwake(false);setTarget(current=>randomLockAngle(current));return;}
    let velocity=0,last=performance.now();
    const step=(now:number)=>{const dt=Math.min((now-last)/1000,.032);last=now;const position=angleValue.current;velocity+=(-36*(position-HOME_ANGLE)-12*velocity)*dt;const next=position+velocity*dt;commitAngle(next);if(Math.abs(next-HOME_ANGLE)<.08&&Math.abs(velocity)<.12){commitAngle(HOME_ANGLE);returnFrame.current=null;setReturning(false);setAwake(false);setTarget(current=>randomLockAngle(current));return;}returnFrame.current=requestAnimationFrame(step);};
    returnFrame.current=requestAnimationFrame(step);
  },[commitAngle]);
  useEffect(()=>{if(!locked)return;const timer=setTimeout(returnHome,4000);return()=>clearTimeout(timer);},[locked,returnHome]);
  function bearing(e:PointerEvent<HTMLDivElement>){const r=e.currentTarget.getBoundingClientRect();return Math.atan2(e.clientY-r.top-r.height/2,e.clientX-r.left-r.width/2)*180/Math.PI;}
  const noise=locked?0:Math.min(1,Math.abs(angle-target)/Math.max(target,1));
  const wave=Array.from({length:101},(_,i)=>`${i*3},${40+Math.sin(i*.24)*12+noise*(Math.sin(i*2.13)*11+Math.cos(i*.87)*8)}`).join(' ');
  return <div className="login-page login-play" onPointerDown={e=>{
    if((e.target as HTMLElement).closest('button,input,form,a,[role="slider"]'))return;
    const r=e.currentTarget.getBoundingClientRect(),id=++serial.current;
    setPings(p=>[...p.slice(-4),{id,x:e.clientX-r.left,y:e.clientY-r.top}]);later(()=>setPings(p=>p.filter(v=>v.id!==id)),1800);
  }}>
    <button type="button" className={`brand secret-brand ${held?'held':''}`} aria-label="SIGNAL — hold to reveal circuit" onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);setHeld(true);}} onPointerUp={()=>setHeld(false)} onPointerCancel={()=>setHeld(false)} onLostPointerCapture={()=>setHeld(false)} onKeyDown={e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();setHeld(true);}}} onKeyUp={()=>setHeld(false)} onBlur={()=>setHeld(false)}>
      <svg viewBox="0 0 80 40" aria-hidden="true"><path className="circuit-a" d="M2 22H20L28 6L40 34L49 14L56 22H78"/><path className="circuit-b" d="M2 22H20V8H60V22H78"/><path className="circuit-c" d="M2 22H12V36H68V22H78"/><circle cx="2" cy="22" r="2"/><circle cx="78" cy="22" r="2"/></svg>SIGNAL<span>v2</span>
    </button>
    <div ref={dial} className={`signal-tuner ${awake?'awake':''} ${locked?'locked':''} ${returning?'returning':''}`} role="slider" tabIndex={0} aria-label="Tune signal frequency" aria-valuemin={0} aria-valuemax={270} aria-valuenow={Math.round(angle)} aria-valuetext={locked?'Signal locked':`${(88+angle/10).toFixed(1)} MHz`} onPointerDown={e=>{if(e.button!==0||locked)return;stopReturn();e.currentTarget.setPointerCapture(e.pointerId);drag.current=bearing(e);setAwake(true);}} onPointerMove={e=>{if(drag.current===null||locked)return;const next=bearing(e),delta=((next-drag.current+540)%360)-180;drag.current=next;tune(angleValue.current+delta);}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onLostPointerCapture={()=>{drag.current=null;}} onKeyDown={e=>{if(['ArrowLeft','ArrowDown','ArrowRight','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();if(!locked){stopReturn();tune(e.key==='Home'?HOME_ANGLE:e.key==='End'?270:angleValue.current+(['ArrowLeft','ArrowDown'].includes(e.key)?-3:3));}}}}>
      <svg className="tuner-dial" viewBox="0 0 600 600" aria-hidden="true"><circle cx="300" cy="300" r="285"/><circle cx="300" cy="300" r="222" strokeDasharray="1 13"/><circle cx="300" cy="300" r="126"/>{Array.from({length:60},(_,i)=><path key={i} transform={`rotate(${i*6} 300 300)`} d={`M300 15V${i%5===0?32:22}`}/>)}<g transform={`rotate(${angle} 300 300)`}><path className="dial-needle" d="M300 9V43"/><circle className="dial-dot" cx="300" cy="78" r="4"/></g></svg>
      <div className="tuner-core"><svg viewBox="0 0 300 80" aria-hidden="true"><polyline points={wave}/></svg><span className="tuner-frequency">{(88+angle/10).toFixed(1)} MHz</span><span className="tuner-status" role="status">{locked?'SIGNAL LOCKED':awake?'SEARCHING…':''}</span><small>{locked?'someone is listening':''}</small></div>
      {locked&&<i className="lock-echo" aria-hidden="true"/>}
    </div>
    {pings.map(p=><div key={p.id} className="signal-ping" style={{left:p.x,top:p.y}} aria-hidden="true"><i/><i/></div>)}
    {pings.length>0&&<div className="ring-echo" key={pings[pings.length-1].id} aria-hidden="true"/>}
    {children}
  </div>;
}
