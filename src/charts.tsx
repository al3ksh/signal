import { useState } from 'react';

export type Point = {time:number;cpu:number;memory:number;temperature:number|null;rx:number;tx:number;gap?:boolean};
export function Chart({points,field,color='var(--lime)',height=80,max,label,markerTime}:{points:Point[];field:'cpu'|'memory'|'temperature'|'rx'|'tx';color?:string;height?:number;max?:number;label:string;markerTime?:number|null}) {
  const [hover,setHover]=useState<number|null>(null);
  const vals=points.map(p=>p[field]);
  const ceiling=max || Math.max(...vals.filter((v):v is number=>v!==null),1)*1.2;
  const first=points[0]?.time||0, span=Math.max(1,(points.at(-1)?.time||first)-first);
  const x=(p:Point)=>(p.time-first)/span*600;
  const path=points.map((p,i)=>p[field]===null?'':`${i===0||p.gap||points[i-1][field]===null?'M':'L'}${x(p)},${height-8-p[field]!/ceiling*(height-16)}`).join(' ');
  const h=hover===null?null:points[hover];
  const unit=field==='temperature'?'°C':'%';
  return <div className="chart" style={{height}} onPointerMove={e=>{if(!points.length)return;const r=e.currentTarget.getBoundingClientRect();const target=first+(e.clientX-r.left)/r.width*span;let closest=0;points.forEach((p,i)=>{if(Math.abs(p.time-target)<Math.abs(points[closest].time-target))closest=i;});setHover(closest);}} onPointerLeave={()=>setHover(null)} tabIndex={0} role="group" aria-label={`${label}. Use left and right arrows to inspect samples.`} onFocus={()=>{if(points.length)setHover(points.length-1);}} onBlur={()=>setHover(null)} onKeyDown={e=>{if(!points.length)return;if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();setHover(v=>e.key==='Home'?0:e.key==='End'?points.length-1:Math.max(0,Math.min(points.length-1,(v??points.length-1)+(e.key==='ArrowLeft'?-1:1))));}}}>
    <svg viewBox={`0 0 600 ${height}`} preserveAspectRatio="none" role="img" aria-label={label}>
      {[.25,.5,.75].map(y=><line key={y} x1="0" x2="600" y1={height*y} y2={height*y} stroke="currentColor" strokeDasharray="2 6"/>)}
      {points.length>1&&<path d={path} fill="none" stroke={color} strokeWidth="1.7" vectorEffect="non-scaling-stroke"/>}
      {markerTime&&markerTime>=first&&markerTime<=first+span&&<line className="incident-marker" x1={(markerTime-first)/span*600} x2={(markerTime-first)/span*600} y1="0" y2={height} stroke="var(--red)" strokeWidth="1" strokeDasharray="2 3" vectorEffect="non-scaling-stroke"/>}
      {h&&<line x1={x(h)} x2={x(h)} y1="0" y2={height} stroke={color} strokeDasharray="3 4"/>}
    </svg>
    {h&&<span className="chart-tip" role="status" aria-live="polite">{new Date(h.time*1000).toLocaleString('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})} / {h[field]===null?'No sensor':field==='rx'||field==='tx'?`${(h[field]/1024).toFixed(1)} KiB/s`:`${h[field]!.toFixed(1)}${unit}`}</span>}
    {!vals.some(v=>v!==null)?<span className="chart-empty">{field==='temperature'&&points.length?'Temperature sensor unavailable':'Collecting signal history…'}</span>:points.length<2?<span className="chart-empty">Waiting for the next sample…</span>:null}
  </div>;
}
