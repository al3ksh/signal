import { useEffect, useRef, useState } from 'react';
import { ArrowCounterClockwise, Check, Palette, X } from '@phosphor-icons/react';

export type Theme = { name: string; background: string; accent: string; motion: 'full' | 'reduced' };
export const presets: Theme[] = [
  { name: 'Carbon', background: '#0d0e10', accent: '#d4d7de', motion: 'full' },
  { name: 'Glacier', background: '#0c1117', accent: '#83ceff', motion: 'full' },
  { name: 'Ember', background: '#15100d', accent: '#ffba83', motion: 'full' },
  { name: 'Iris', background: '#111019', accent: '#c0afff', motion: 'full' },
];
const KEY = 'signal.theme.v2';
const hex = /^#[0-9a-f]{6}$/i;
function rgb(value: string) { return [1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16)); }
function mix(a: string, b: string, t: number) { return '#' + rgb(a).map((v, i) => Math.round(v + (rgb(b)[i] - v) * t).toString(16).padStart(2, '0')).join(''); }
function luminance(color: string) { return rgb(color).map(v => { const n = v / 255; return n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4; }).reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0); }
export function contrast(a: string, b: string) { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); }
function readable(color: string, background: string | string[], minimum: number) {
  const backgrounds = Array.isArray(background) ? background : [background];
  const score = (value: string) => Math.min(...backgrounds.map(bg => contrast(value, bg)));
  const target = score('#000000') > score('#ffffff') ? '#000000' : '#ffffff';
  let result = color;
  for (let n = 0; n <= 100 && score(result) < minimum; n++) result = mix(color, target, n / 100);
  return result;
}
export function themeTokens(theme: Theme) {
  const light = luminance(theme.background) > .179;
  const ink = light ? '#111214' : '#f0f1f3';
  let surface = mix(theme.background, ink, .045);
  const extreme = light ? '#000000' : '#ffffff';
  if (contrast(extreme, surface) < 4.5) surface = mix(theme.background, light ? '#ffffff' : '#000000', .06);
  const backgrounds = [theme.background, surface];
  const accent = readable(theme.accent, backgrounds, 5);
  const candidateSoft = mix(theme.background, accent, .11);
  const accentSoft = contrast(accent, candidateSoft) >= 4.5 ? candidateSoft : surface;
  const text = readable(ink, backgrounds, 7);
  return {
    '--bg': theme.background, '--surface': surface, '--surface-hover': mix(theme.background, ink, .09),
    '--text': text, '--muted': readable(mix(theme.background, ink, .56), backgrounds, 4.5),
    '--line': mix(theme.background, ink, .16), '--subtle-line': mix(theme.background, ink, .09),
    '--lime': accent, '--accent': accent, '--accent-soft': accentSoft,
    '--accent-line': mix(theme.background, accent, .34), '--accent-ink': contrast(accent, '#000000') > contrast(accent, '#ffffff') ? '#000000' : '#ffffff',
    '--track': mix(theme.background, ink, .14), '--sidebar': mix(theme.background, light ? '#ffffff' : '#000000', .23),
    '--red': readable(light ? '#ac322e' : '#f5a495', surface, 4.5), '--bad-bg': mix(theme.background, '#d75d55', .10),
    '--bad-line': mix(theme.background, '#d75d55', .38), '--color-scheme': light ? 'light' : 'dark',
  };
}
function restoreTheme(): Theme {
  try { const stored = JSON.parse(localStorage.getItem(KEY) || 'null'); if (stored && hex.test(stored.background) && hex.test(stored.accent)) return { name: typeof stored.name === 'string' ? stored.name : 'Custom', background: stored.background, accent: stored.accent, motion: stored.motion === 'reduced' ? 'reduced' : 'full' }; } catch { /* Storage may be disabled. */ }
  return presets[0];
}
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(restoreTheme);
  const [systemReduced, setSystemReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => { const media = matchMedia('(prefers-reduced-motion: reduce)'); const change = () => setSystemReduced(media.matches); media.addEventListener('change', change); return () => media.removeEventListener('change', change); }, []);
  useEffect(() => { const change = () => { document.documentElement.dataset.visibility = document.hidden ? 'hidden' : 'visible'; }; change(); document.addEventListener('visibilitychange', change); return () => document.removeEventListener('visibilitychange', change); }, []);
  useEffect(() => {
    const root = document.documentElement;
    Object.entries(themeTokens(theme)).forEach(([key, value]) => root.style.setProperty(key, value));
    root.dataset.motion = theme.motion === 'reduced' || systemReduced ? 'reduced' : 'full';
    root.dataset.theme = theme.name;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme.background);
    try { localStorage.setItem(KEY, JSON.stringify(theme)); } catch { /* The current tab still keeps its theme. */ }
  }, [theme, systemReduced]);
  return { theme, setTheme, motion: theme.motion === 'full' && !systemReduced };
}

export function ThemeEditor({ theme, onChange, onClose }: { theme: Theme; onChange: (theme: Theme) => void; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState({ background: theme.background, accent: theme.accent });
  const tokens = themeTokens(theme);
  useEffect(() => { ref.current?.showModal(); }, []);
  useEffect(() => setDraft({ background: theme.background, accent: theme.accent }), [theme.background, theme.accent]);
  function update(key: 'background' | 'accent', value: string) { setDraft(v => ({ ...v, [key]: value })); if (hex.test(value)) onChange({ ...theme, name: 'Custom', [key]: value }); }
  return <dialog className="theme-dialog" ref={ref} onCancel={onClose} aria-labelledby="theme-title" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
    <header><div><Palette size={20} /><h2 id="theme-title">Make it yours.</h2></div><button className="icon-button" onClick={onClose} aria-label="Close theme editor"><X size={21} /></button></header>
    <p className="theme-description">Your console, your atmosphere. Changes preview instantly.</p>
    <div className="theme-preview" aria-label="Live theme preview"><div className="preview-orbit"><i /><i /><i /><span /></div><div><small>PERSONAL FREQUENCY</small><strong>{theme.name}</strong><span><i className="dot" /> All systems in your orbit.</span></div></div>
    <fieldset><legend>START WITH A PRESET</legend><div className="theme-presets">{presets.map(p => <button key={p.name} aria-pressed={theme.name === p.name} onClick={() => onChange({ ...p, motion: theme.motion })} style={{ '--swatch-bg': p.background, '--swatch-accent': p.accent } as React.CSSProperties}><span className="preset-swatch"><i /><i /><i />{theme.name === p.name && <Check size={15} />}</span><span>{p.name}</span></button>)}</div></fieldset>
    <fieldset><legend>TUNE YOUR COLORS</legend>{(['background', 'accent'] as const).map(key => <div className="color-control" key={key}><label htmlFor={`theme-${key}`}>{key === 'background' ? 'Canvas' : 'Accent'}</label><input type="color" aria-label={`${key === 'background' ? 'Canvas' : 'Accent'} color picker`} value={theme[key]} onChange={e => update(key, e.target.value)} /><input id={`theme-${key}`} aria-label={`${key === 'background' ? 'Canvas' : 'Accent'} hex color`} value={draft[key]} spellCheck={false} maxLength={7} onChange={e => update(key, e.target.value)} onBlur={() => setDraft(v => ({ ...v, [key]: theme[key] }))} /></div>)}<p className="theme-note">Text and accent contrast adapt automatically to your canvas.</p></fieldset>
    <fieldset><legend>MOTION</legend><div className="motion-options">{(['full', 'reduced'] as const).map(mode => <button key={mode} aria-pressed={theme.motion === mode} className={theme.motion === mode ? 'active' : ''} onClick={() => onChange({ ...theme, motion: mode })}>{mode === 'full' ? 'Full signal' : 'Keep it quiet'}{theme.motion === mode && <Check size={13} />}</button>)}</div><p className="theme-note">Your device’s reduced motion preference always takes priority.</p></fieldset>
    <footer><span><Check size={13} /> Saved on this device <small>{contrast(tokens['--text'], tokens['--surface']).toFixed(1)}:1 text contrast</small></span><button onClick={() => onChange(presets[0])}><ArrowCounterClockwise size={14} /> Reset</button></footer>
  </dialog>;
}
