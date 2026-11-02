import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Pulse } from '@phosphor-icons/react';
import './login-play.css';

type Mode = 'idle'|'busy'|'error';
type Readout = {fx:number;fy:number;phase:number;lock:string|null;hold:boolean};
const TAU = Math.PI*2, SPAN = TAU*3, POINTS = 1500, SNAP = .14;
const gcd = (a:number,b:number):number => b ? gcd(b,a%b) : a;
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.motion==='reduced';

// A phosphor oscilloscope: the pointer sets the X/Y channel frequencies,
// integer ratios snap into closed Lissajous figures, typing excites the beam.
export function LoginPlay({children,mode='idle'}:{children:ReactNode;mode?:Mode}) {
  const canvas = useRef<HTMLCanvasElement>(null), scope = useRef<HTMLDivElement>(null);
  const modeRef = useRef<Mode>(mode), kick = useRef(0), alarm = useRef(0), hold = useRef(false), redraw = useRef<() => void>(() => {});
  const [readout,setReadout] = useState<Readout>({fx:3,fy:2,phase:.5,lock:'3:2',hold:false});
  useEffect(() => {modeRef.current = mode; if(mode==='error'){kick.current = 1.6; alarm.current = 1;} redraw.current();}, [mode]);

  useEffect(() => {
    const el = canvas.current!, box = scope.current!, ctx = el.getContext('2d')!;
    let w = 0, h = 0, frame = 0, last = performance.now(), lastReadout = 0, lastColor = 0;
    let accent = '#d4d7de', red = '#f5a495', moved = -1e9;
    const s = {fx:3, fy:2, tx:3, ty:2, phase:TAU/4, head:0, error:0};
    const colors = () => {const css = getComputedStyle(document.documentElement); accent = css.getPropertyValue('--accent').trim()||accent; red = css.getPropertyValue('--red').trim()||red;};
    const size = () => {const r = el.getBoundingClientRect(), d = Math.min(devicePixelRatio||1,2); w = r.width; h = r.height; el.width = Math.round(w*d); el.height = Math.round(h*d); ctx.setTransform(d,0,0,d,0,0); draw(0,true);};
    const snapped = (v:number) => Math.abs(v-Math.round(v))<SNAP ? Math.round(v) : v;
    const lockOf = () => Number.isInteger(s.tx)&&Number.isInteger(s.ty)&&Math.abs(s.fx-s.tx)<.004&&Math.abs(s.fy-s.ty)<.004 ? (g => s.tx/g+':'+s.ty/g)(gcd(s.tx,s.ty)) : null;

    function step(dt:number, now:number) {
      const m = modeRef.current;
      if(m==='busy'){s.tx = 1; s.ty = 1;}
      else if(!hold.current && now-moved>4000){const t = now/1000; s.tx = snapped(3+Math.sin(t*.11)*1.9); s.ty = snapped(2.6+Math.cos(t*.07)*1.5);}
      const ease = 1-Math.exp(-dt*(m==='busy'?6:3.2));
      s.fx += (s.tx-s.fx)*ease; s.fy += (s.ty-s.fy)*ease;
      if(Math.abs(s.fx-s.tx)<.002)s.fx = s.tx; if(Math.abs(s.fy-s.ty)<.002)s.fy = s.ty;
      // Locked figures settle into a clean pose; free-running ones keep rolling.
      if(m==='busy')s.phase += dt*5.5;
      else if(lockOf()&&!hold.current){const d = ((TAU/4-s.phase)%TAU+TAU*1.5)%TAU-Math.PI; s.phase += d*(1-Math.exp(-dt*2.4));}
      else if(!hold.current)s.phase += dt*.42;
      s.head = (s.head+dt*TAU*.55)%SPAN;
      kick.current *= Math.exp(-dt*4.5);
      s.error = Math.max(alarm.current,s.error*Math.exp(-dt*1.4)); alarm.current = 0;
    }

    function draw(dt:number, still=false) {
      if(!w)return;
      const reduced = still||reducedMotion(), color = s.error>.05 ? red : accent;
      if(reduced){ctx.clearRect(0,0,w,h);}
      else {ctx.globalCompositeOperation = 'destination-out'; ctx.fillStyle = `rgba(0,0,0,${Math.min(1,dt*9)})`; ctx.fillRect(0,0,w,h); ctx.globalCompositeOperation = 'source-over';}
      const ax = w*.41, ay = h*.39, cx = w/2, cy = h/2, noise = kick.current*.09+s.error*.06;
      const at = (t:number, i:number) => [cx+ax*(Math.sin(s.fx*t+s.phase)+noise*Math.sin(i*2.71+t*13)), cy-ay*(Math.sin(s.fy*t)+noise*Math.cos(i*1.93-t*11))];
      ctx.strokeStyle = color; ctx.lineJoin = 'round';
      ctx.globalAlpha = reduced ? .7 : .2; ctx.lineWidth = 1; ctx.beginPath();
      for(let i=0;i<=POINTS;i++){const [x,y] = at(i/POINTS*SPAN,i); i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
      ctx.stroke();
      if(!reduced){
        ctx.shadowColor = color; ctx.shadowBlur = 9; ctx.lineWidth = 1.6;
        for(let k=0;k<6;k++){
          ctx.globalAlpha = (k+1)/6*.85; ctx.beginPath();
          for(let j=0;j<=24;j++){const t = s.head-(6-k)*.07+j*.07/24, [x,y] = at(t,Math.round(t/SPAN*POINTS)); j?ctx.lineTo(x,y):ctx.moveTo(x,y);}
          ctx.stroke();
        }
        const [hx,hy] = at(s.head,0); ctx.globalAlpha = 1; ctx.fillStyle = color; ctx.beginPath(); ctx.arc(hx,hy,2.1,0,TAU); ctx.fill(); ctx.shadowBlur = 0;
      }
      ctx.globalAlpha = 1;
    }

    function loop(now:number) {
      const dt = Math.min((now-last)/1000,.05); last = now;
      if(now-lastColor>1000){colors(); lastColor = now;}
      step(dt,now); draw(dt);
      if(now-lastReadout>120){lastReadout = now; setReadout({fx:s.fx,fy:s.fy,phase:(s.phase%TAU+TAU)%TAU/Math.PI,lock:lockOf(),hold:hold.current});}
      frame = requestAnimationFrame(loop);
    }
    const start = () => {cancelAnimationFrame(frame); if(reducedMotion()||document.hidden){redraw.current(); return;} last = performance.now(); frame = requestAnimationFrame(loop);};
    redraw.current = () => {if(!reducedMotion())return; s.fx = s.tx; s.fy = s.ty; colors(); draw(0,true); setReadout({fx:s.fx,fy:s.fy,phase:(s.phase%TAU)/Math.PI,lock:lockOf(),hold:hold.current});};

    const move = (e:PointerEvent) => {
      if(hold.current||e.pointerType==='touch'&&e.target!==box&&!box.contains(e.target as Node))return;
      moved = performance.now();
      s.tx = snapped(1+Math.min(1,Math.max(0,e.clientX/innerWidth))*4); s.ty = snapped(1+Math.min(1,Math.max(0,1-e.clientY/innerHeight))*4);
      redraw.current();
    };
    const visibility = () => document.hidden ? cancelAnimationFrame(frame) : start();
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const observer = new ResizeObserver(size); observer.observe(el);
    const attrs = new MutationObserver(start); attrs.observe(document.documentElement,{attributes:true,attributeFilter:['data-motion']});
    addEventListener('pointermove',move,{passive:true}); document.addEventListener('visibilitychange',visibility); motion.addEventListener('change',start);
    colors(); size(); start();
    return () => {cancelAnimationFrame(frame); observer.disconnect(); attrs.disconnect(); removeEventListener('pointermove',move); document.removeEventListener('visibilitychange',visibility); motion.removeEventListener('change',start);};
  }, []);

  const status = mode==='busy' ? 'HANDSHAKE' : mode==='error' ? 'REJECTED' : readout.hold ? 'HOLD' : readout.lock ? 'LOCK '+readout.lock : 'FREE RUN';
  return <div className="login-page login-play" onInput={() => {kick.current = Math.min(1.2,kick.current+.45);}}>
    <a className="brand" href="/"><Pulse size={30} weight="bold"/>SIGNAL<span>v2</span></a>
    <div ref={scope} className={['phosphor-scope',readout.lock&&mode==='idle'?'locked':'',mode].join(' ')} aria-hidden="true"
      onPointerDown={e => {if(e.button!==0)return; hold.current = !hold.current; setReadout(r => ({...r,hold:hold.current}));}}>
      <svg className="scope-graticule" viewBox="0 0 500 400" preserveAspectRatio="none">
        {Array.from({length:9},(_,i) => <path key={'v'+i} d={`M${(i+1)*50} 0V400`}/>)}
        {Array.from({length:7},(_,i) => <path key={'h'+i} d={`M0 ${(i+1)*50}H500`}/>)}
        <path className="scope-axis" d="M250 0V400M0 200H500"/>
        {Array.from({length:49},(_,i) => <path key={'t'+i} className="scope-axis" d={`M${i*10+10} 196V204M246 ${i*10+10}H254`}/>)}
      </svg>
      <canvas ref={canvas}/>
      <span className="scope-label tl">CH1 X {readout.fx.toFixed(2)}</span>
      <span className="scope-label tr">CH2 Y {readout.fy.toFixed(2)}</span>
      <span className="scope-label bl">φ {readout.phase.toFixed(2)}π</span>
      <span className="scope-label br scope-status">{status}</span>
    </div>
    <p className="scope-hint" aria-hidden="true">Move to tune · find a closed figure · click to hold</p>
    {children}
  </div>;
}
