import { useEffect, useRef, useState } from 'react';
import { CirclesFour, GridFour } from '@phosphor-icons/react';
import type { Container } from './main';

const good = (c: Container) => c.state === 'running' && c.health !== 'unhealthy';
export function Orbit({ hostName, arch, containers, onSelect, paused, motion, highlighted, onHighlight }: { hostName:string; arch:string; containers: Container[]; onSelect: (c: Container) => void; paused: boolean; motion: boolean; highlighted: string | null; onHighlight: (id: string | null) => void }) {
  const [layout, setLayout] = useState<'orbit' | 'grid'>('orbit');
  const [visible, setVisible] = useState(true);
  const [hidden, setHidden] = useState(document.hidden);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { const observer = new IntersectionObserver(entries => setVisible(entries[0].isIntersecting), { threshold: .1 }); if (ref.current) observer.observe(ref.current); const change = () => setHidden(document.hidden); document.addEventListener('visibilitychange', change); return () => { observer.disconnect(); document.removeEventListener('visibilitychange', change); }; }, []);
  const active = motion && !paused && visible && !hidden;
  const selected = containers.find(c => c.id === highlighted);
  const cols = 4, rows = Math.ceil(containers.length / cols);
  const locations = containers.map((c, i) => {
    const a = i / Math.max(1, containers.length) * Math.PI * 2 - Math.PI / 2, r = i % 3 === 0 ? 116 : 87;
    return { c, x: layout === 'orbit' ? 220 + Math.cos(a) * r : 94 + (i % cols) * 84, y: layout === 'orbit' ? 158 + Math.sin(a) * r : 91 + Math.floor(i / cols) * Math.min(66, 180 / Math.max(1, rows - 1)) };
  });
  return <div className="orbit-interactive" ref={ref}>
    <div className="map-controls"><span>{layout === 'orbit' ? 'HOST RELATIONSHIPS' : 'CONTAINER MATRIX'}</span><div><button title="Orbit layout" aria-label="Orbit layout" aria-pressed={layout === 'orbit'} onClick={() => setLayout('orbit')}><CirclesFour size={15} /></button><button title="Grid layout" aria-label="Grid layout" aria-pressed={layout === 'grid'} onClick={() => setLayout('grid')}><GridFour size={15} /></button></div></div>
    <div className={`orbit orbit-v2 ${!active ? 'is-paused' : ''} ${layout === 'grid' ? 'matrix-mode' : ''}`}>
      <div className="orbit-grid" />
      <svg viewBox="0 0 440 340" role="img" aria-label={`Interactive container map connected to ${hostName}`}>
        <g className="orbit-rings">{[58, 87, 116, 143].map((r, i) => <circle key={r} cx="220" cy="158" r={r} fill="none" stroke="currentColor" strokeWidth=".7" strokeDasharray={i === 1 ? '2 6' : undefined} />)}<circle className="orbit-sweep" cx="220" cy="158" r="116" fill="none" stroke="var(--accent)" strokeWidth="1.2" strokeDasharray="48 681" /><path d="M220 0V28M220 288V310M50 158H83M357 158H390" stroke="currentColor" /></g>
        {locations.map(({ c, x, y }, i) => <g key={c.id} className={`map-node ${highlighted === c.id ? 'focused' : ''} ${highlighted && highlighted !== c.id ? 'unfocused' : ''}`}>
          {layout === 'orbit' && <line x1="220" y1="158" x2={x} y2={y} className={good(c) ? 'connection' : 'connection bad'} style={{ opacity: highlighted === c.id ? .85 : .2 }} />}
          {layout === 'orbit' && active && good(c) && i % 3 === 0 && <circle r="1.7" fill="var(--accent)" opacity=".6"><animateMotion path={`M220,158 L${x},${y}`} dur={`${3.5 + i * .2}s`} begin={`${-i * .4}s`} repeatCount="indefinite" /></circle>}
          <g className="node-position" style={{ transform: `translate(${x}px,${y}px)` }}><circle className="node-halo" r={highlighted === c.id ? 17 : 10} fill={good(c) ? 'var(--accent)' : 'var(--red)'} opacity={highlighted === c.id ? .16 : .045} /><circle className="node-outline" r={layout === 'grid' ? 16 : 8} fill="var(--bg)" stroke={good(c) ? 'var(--accent-line)' : 'var(--red)'} /><circle r={3 + Math.min(5, (c.cpu || 0) / 5)} fill={good(c) ? 'var(--accent)' : 'var(--red)'} /><text y="33" textAnchor="middle" className="matrix-name">{c.service || c.name.split('-').slice(-1)[0]}</text></g>
        </g>)}
        <g className="orbit-hub"><circle cx="220" cy="158" r="44" fill="var(--surface)" stroke="var(--line)" /><text x="220" y="155" className="orbit-value" textAnchor="middle">{containers.filter(good).length.toString().padStart(2, '0')}</text><text x="220" y="174" className="orbit-caption" textAnchor="middle">ONLINE</text></g>
      </svg>
      {locations.map(({ c, x, y }) => <button key={c.id} className={`orbit-hit ${highlighted === c.id ? 'is-highlighted' : ''}`} style={{ left: `${x / 440 * 100}%`, top: `${y / 340 * 100}%` }} aria-label={`Inspect ${c.name}`} onPointerEnter={() => onHighlight(c.id)} onPointerLeave={() => onHighlight(null)} onFocus={() => onHighlight(c.id)} onBlur={() => onHighlight(null)} onClick={() => onSelect(c)} />)}
      <span className="orbit-coord top">N / 00°</span><span className="orbit-coord left">HOST<br />{hostName}</span><span className="orbit-coord right">{arch}</span>
      <div className="orbit-readout" aria-live="polite">{selected ? <><strong>{selected.name}</strong><span>{selected.cpu == null ? 'CPU pending' : `${selected.cpu.toFixed(1)}% CPU`} / {selected.memory == null ? 'RAM pending' : `${(selected.memory / 1024 ** 2).toFixed(1)} MiB`}</span></> : <><strong><i className="dot" /> {containers.length} containers. One little universe.</strong><span>Hover to trace. Click to inspect.</span></>}</div>
    </div>
  </div>;
}
