import { useCallback, useEffect, useRef, useState, type ReactNode, type PointerEvent } from 'react';
import { Pulse } from '@phosphor-icons/react';
import './login-play.css';

function randomLockAngle(previous?:number){
  let next=108+Math.random()*134;
  if(previous!==undefined&&Math.abs(next-previous)<38)next=previous<175?Math.min(242,previous+55):Math.max(108,previous-55);
  return Math.round(next);
}
export function LoginPlay({children}:{children:ReactNode}) {
  const [angle,setAngle]=useState(0),[target,setTarget]=useState(()=>randomLockAngle()),[awake,setAwake]=useState(false),[locked,setLocked]=useState(false);
  const drag=useRef<number|null>(null),angleValue=useRef(0),frame=useRef<number|null>(null),timer=useRef<ReturnType<typeof setTimeout>|null>(null),armed=useRef(true),isLocked=useRef(false);
  const commit=useCallback((value:number)=>{angleValue.current=value;setAngle(value);},[]);
  const cancel=useCallback(()=>{if(frame.current!==null)cancelAnimationFrame(frame.current);frame.current=null;if(timer.current!==null)clearTimeout(timer.current);timer.current=null;},[]);
  useEffect(()=>cancel,[cancel]);
  const returnHome=useCallback(()=>{
    isLocked.current=false;setLocked(false);drag.current=null;
    const finish=()=>{commit(0);frame.current=null;setAwake(false);setTarget(current=>randomLockAngle(current));armed.current=true;};
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches||document.documentElement.dataset.motion==='reduced'){finish();return;}
    let velocity=0,last=performance.now();
    const step=(now:number)=>{const dt=Math.min((now-last)/1000,.032);last=now;velocity+=(-36*angleValue.current-12*velocity)*dt;const next=angleValue.current+velocity*dt;commit(next);if(Math.abs(next)<.08&&Math.abs(velocity)<.12){finish();return;}frame.current=requestAnimationFrame(step);};
    frame.current=requestAnimationFrame(step);
  },[commit]);
  function tune(value:number){
    cancel();setAwake(true);
    const previous=angleValue.current,next=Math.max(0,Math.min(270,value));
    // Moving away from a lock must not immediately capture it again.
    if(isLocked.current){isLocked.current=false;setLocked(false);armed.current=false;}
    if(armed.current&&((previous<=target&&next>=target)||(previous>=target&&next<=target)||Math.abs(next-target)<=6)){
      commit(target);isLocked.current=true;armed.current=false;setLocked(true);drag.current=null;timer.current=setTimeout(returnHome,2000);return;
    }
    commit(next);if(Math.abs(next-target)>12)armed.current=true;
  }
  function bearing(e:PointerEvent<HTMLDivElement>){const r=e.currentTarget.getBoundingClientRect();return Math.atan2(e.clientY-r.top-r.height/2,e.clientX-r.left-r.width/2)*180/Math.PI;}
  function release(){drag.current=null;if(isLocked.current){cancel();timer.current=setTimeout(returnHome,2000);}}
  const noise=locked?0:Math.min(1,Math.abs(angle-target)/Math.max(target,1));
  const wave=Array.from({length:101},(_,i)=>[i*3,40+Math.sin(i*.24)*12+noise*(Math.sin(i*2.13)*11+Math.cos(i*.87)*8)].join(',')).join(' ');
  return <div className="login-page login-play">
    <a className="brand" href="/"><Pulse size={30} weight="bold"/>SIGNAL<span>v2</span></a>
    <div className={['signal-tuner',awake?'awake':'',locked?'locked':''].join(' ')} role="slider" tabIndex={0} aria-label="Tune signal frequency" aria-valuemin={0} aria-valuemax={270} aria-valuenow={Math.round(angle)} aria-valuetext={locked?'Signal locked':(88+angle/10).toFixed(1)+' MHz'}
      onPointerDown={e=>{if(e.button!==0)return;cancel();e.currentTarget.setPointerCapture(e.pointerId);drag.current=bearing(e);setAwake(true);}}
      onPointerMove={e=>{if(drag.current===null)return;const next=bearing(e),delta=((next-drag.current+540)%360)-180;if(Math.abs(delta)<.05)return;drag.current=next;tune(angleValue.current+delta);}}
      onPointerUp={release} onPointerCancel={release} onLostPointerCapture={()=>{drag.current=null;}}
      onKeyDown={e=>{if(['ArrowLeft','ArrowDown','ArrowRight','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();tune(e.key==='Home'?0:e.key==='End'?270:angleValue.current+(['ArrowLeft','ArrowDown'].includes(e.key)?-3:3));}}}>
      <svg className="tuner-dial" viewBox="0 0 600 600" aria-hidden="true"><circle cx="300" cy="300" r="285"/><circle cx="300" cy="300" r="222" strokeDasharray="1 13"/><circle className="tuner-inner-ring" cx="300" cy="300" r="126"/>{Array.from({length:60},(_,i)=><path key={i} transform={'rotate('+i*6+' 300 300)'} d={'M300 15V'+(i%5===0?32:22)}/>)}<g transform={'rotate('+angle+' 300 300)'}><path className="dial-needle" d="M300 9V43"/><circle className="dial-dot" cx="300" cy="78" r="4"/></g></svg>
      <div className="tuner-core"><svg viewBox="0 0 300 80" aria-hidden="true"><polyline points={wave}/></svg><span className="tuner-frequency">{(88+angle/10).toFixed(1)} MHz</span><span className="tuner-status" role="status">{locked?'SIGNAL LOCKED':awake?'SEARCHING…':''}</span></div>
    </div>
    {children}
  </div>;
}
