import { Cpu, HardDrives, Clock, Lightning, Memory, WarningOctagon, ChartLineUp } from '@phosphor-icons/react';
import type { ReactNode } from 'react';
import './health.css';

export type PiHealth = {
  power:{now:string[];sinceBoot:string[];raw:string;coreVolts?:number|null}|null;
  sd:{device:string;errors:number|null;lastErrorTime?:number|null;lifetimeWriteKbytes:number|null;writesPerDay?:number|null}|null;
  rootReadOnly:boolean|null;inodes:{total:number;free:number}|null;clockSynced:boolean|null;
  memory:{zram:{original:number;compressed:number;used:number}|null;swapTotal:number|null;swapUsed:number|null;majorFaultsPerSec:number|null;pressure?:{cpu?:number;memory?:number;io?:number}};
  failedUnits:string[]|null;diskForecast:{days:number;slopePerDay:number}|null;checkedAt:number;
};
type Tone='good'|'warn'|'bad'|'unknown';
const words:Record<string,string>={'under-voltage':'under-voltage','arm-frequency-capped':'frequency capped','throttled':'throttled','soft-temperature-limit':'soft temperature limit'};
const list=(items:string[])=>items.map(item=>words[item]||item).join(', ');
const size=(bytes:number)=>bytes>=1024**3?`${(bytes/1024**3).toFixed(1)} GB`:`${Math.round(bytes/1024**2)} MB`;
const date=(seconds:number)=>new Date(seconds*1000).toLocaleDateString('en-GB',{day:'2-digit',month:'short'});

/** The temperature card's one-line throttling summary, decoded from all get_throttled bits. */
export function throttleText(power:PiHealth['power']|undefined,raw:number|null|undefined){
  if(power)return power.now.length?`Now: ${list(power.now)}`:power.sinceBoot.length?`OK now · since boot: ${list(power.sinceBoot)}`:'No throttling since boot';
  return raw==null?'Throttling data unavailable':raw&15?'Hardware throttling detected':'No active throttling';
}

export function rows(health:PiHealth):{id:string;icon:ReactNode;label:string;tone:Tone;text:string}[]{
  const {power,sd,memory,inodes}=health,result:{id:string;icon:ReactNode;label:string;tone:Tone;text:string}[]=[];
  const powerState:{tone:Tone;text:string}=!power?{tone:'unknown',text:'unavailable'}:power.now.length?{tone:'bad',text:`${list(power.now)} right now`}:power.sinceBoot.length?{tone:'warn',text:`since boot: ${list(power.sinceBoot)} — the PSU or cable may be too weak`}:{tone:'good',text:'OK since boot'};
  result.push({id:'power',icon:<Lightning/>,label:'Power',tone:powerState.tone,text:powerState.text+(power?.coreVolts?` · core ${power.coreVolts.toFixed(2)} V`:'')});
  if(health.rootReadOnly)result.push({id:'sd',icon:<HardDrives/>,label:'SD card',tone:'bad',text:'root filesystem is read-only — the card may be failing'});
  else result.push({id:'sd',icon:<HardDrives/>,label:'SD card',...(!sd||sd.errors==null?{tone:'unknown',text:'unavailable'}:{tone:sd.errors?'warn':'good',text:`${sd.errors} ${sd.errors===1?'error':'errors'}${sd.errors&&sd.lastErrorTime?` (last ${date(sd.lastErrorTime)})`:''}${sd.writesPerDay!=null?` · ~${size(sd.writesPerDay)} written/day`:sd.lifetimeWriteKbytes!=null?` · ${size(sd.lifetimeWriteKbytes*1024)} written in total`:''}`})});
  result.push({id:'clock',icon:<Clock/>,label:'Clock',...(health.clockSynced==null?{tone:'unknown',text:'unavailable'}:health.clockSynced?{tone:'good',text:'synced'}:{tone:'warn',text:'not synced — authenticator codes may be rejected'})});
  const faults=memory.majorFaultsPerSec,zram=memory.zram,swap=memory.swapTotal?`swap ${size(memory.swapUsed||0)} of ${size(memory.swapTotal)}`:null;
  const pressure=memory.pressure?Object.entries(memory.pressure).map(([name,value])=>`${name} ${value.toFixed(1)}%`).join(' · '):null;
  result.push({id:'memory',icon:<Memory/>,label:'Memory',tone:faults==null&&!zram&&!swap?'unknown':(faults||0)>=50?'warn':'good',
    text:[zram&&zram.original?`zram ${size(zram.original)} → ${size(zram.compressed)}`:null,swap,faults!=null?`${faults.toFixed(1)} major faults/s`:null,pressure?`pressure 60 s: ${pressure}`:null].filter(Boolean).join(' · ')||'unavailable'});
  result.push({id:'units',icon:<WarningOctagon/>,label:'Failed units',...(health.failedUnits==null?{tone:'unknown',text:'unavailable'}:health.failedUnits.length?{tone:'warn',text:health.failedUnits.join(', ')}:{tone:'good',text:'none'})});
  const lowInodes=inodes&&inodes.free/inodes.total<.1,forecast=health.diskForecast;
  result.push({id:'disk',icon:<ChartLineUp/>,label:'Disk',tone:forecast&&forecast.days<14||lowInodes?'warn':'good',text:[forecast?`full in ~${Math.max(1,Math.round(forecast.days))} days (+${forecast.slopePerDay.toFixed(2)} %/day)`:'not filling up',inodes?`${Math.round((1-inodes.free/inodes.total)*100)}% inodes used`:null].filter(Boolean).join(' · ')});
  return result;
}

export function HealthCard({health}:{health:PiHealth|undefined}){
  if(!health)return null;
  const items=rows(health),worst=items.some(r=>r.tone==='bad')?'bad':items.some(r=>r.tone==='warn')?'warn':'good';
  return <section className={`pi-health is-${worst}`} aria-labelledby="pi-health-title"><header><div><span className="small-label">HOST HEALTH</span><h2 id="pi-health-title"><Cpu size={16}/>Is the Raspberry OK?</h2></div><span className={`pi-verdict is-${worst}`}><i className={`dot ${worst==='bad'?'bad':worst==='warn'?'warn':''}`}/>{worst==='bad'?'Needs attention':worst==='warn'?'Worth a look':'All checks fine'}</span></header>
    <ul>{items.map(item=><li key={item.id} className={`is-${item.tone}`}><span className="pi-icon">{item.icon}</span><i className={`dot ${item.tone==='bad'?'bad':item.tone==='warn'?'warn':item.tone==='unknown'?'neutral':''}`} aria-hidden="true"/><strong>{item.label}</strong><span>{item.text}</span></li>)}</ul></section>;
}
