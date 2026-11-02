import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import './fluent.css';

/* Fluent-style ambient motion. Decorative only: nothing here encodes or imitates measured data. */

const REVEAL = '.app-card,.service-card,.alert-shortcut,.metric,.brief-grid article,.app-actions>button,.confirm-dialog footer button,.command-list>button,.refresh-button,.job-history button,.event';
const reduced = () => document.documentElement.dataset.motion === 'reduced' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** A single indicator that slides to the active navigation item (connected animation). */
export function useNavIndicator(nav: RefObject<HTMLElement | null>, key: string) {
  const [box, setBox] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const element = nav.current;
    if (!element) return;
    const measure = () => {
      const active = element.querySelector<HTMLElement>('button.active');
      setBox(active ? { x: active.offsetLeft, y: active.offsetTop, w: active.offsetWidth, h: active.offsetHeight } : null);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [nav, key]);
  return box;
}

type Trace = { base: number; amp: number; freq: number; speed: number; phase: number; alpha: number };

/** Background oscilloscope field: slow traces, one travelling pulse, a faint sweep. Static when quiet. */
export function AmbientField({ active }: { active: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current, context = canvas?.getContext('2d');
    if (!canvas || !context) return;
    const traces: Trace[] = [
      { base: .16, amp: 26, freq: 1.6, speed: .05, phase: 0, alpha: .22 },
      { base: .38, amp: 38, freq: 1.1, speed: -.035, phase: 2.1, alpha: .15 },
      { base: .66, amp: 30, freq: 2.2, speed: .028, phase: 4.2, alpha: .12 },
      { base: .88, amp: 20, freq: 1.4, speed: -.04, phase: 1.3, alpha: .09 },
    ];
    let width = 0, height = 0, ratio = 1, frame = 0, previous = 0, time = 0, color = '#d4d7de';
    // The traces follow an eased copy of the pointer, so fast moves glide instead of jumping.
    const pointer = { x: -9999, y: -9999, tx: -9999, ty: -9999, strength: 0 };
    const readColor = () => { color = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || color; };
    const resize = () => {
      ratio = Math.min(2, window.devicePixelRatio || 1);
      width = window.innerWidth; height = window.innerHeight;
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw();
    };
    const y = (trace: Trace, x: number) => {
      const u = x / Math.max(1, width) * Math.PI * 2;
      const wave = Math.sin(u * trace.freq + trace.phase + time * trace.speed * 6) * .7 + Math.sin(u * trace.freq * 2.7 - time * trace.speed * 9 + trace.phase) * .3;
      const base = trace.base * height, dx = x - pointer.x, dy = base - pointer.y;
      // Continuous push away from the pointer: zero on the line, strongest ~85px off it, so crossing never flips.
      const bend = pointer.strength * Math.exp(-(dx * dx) / 18000) * (dy / 85) * Math.exp(.5 - (dy * dy) / 14450) * 22;
      return base + wave * trace.amp + bend;
    };
    function draw() {
      context!.clearRect(0, 0, width, height);
      context!.strokeStyle = color; context!.fillStyle = color; context!.lineWidth = 1;
      for (const trace of traces) {
        context!.globalAlpha = trace.alpha;
        context!.beginPath();
        for (let x = 0; x <= width + 8; x += 8) { const value = y(trace, x); if (x === 0) context!.moveTo(x, value); else context!.lineTo(x, value); }
        context!.stroke();
      }
      // A pulse of signal travelling along the first two traces.
      for (const [index, trace] of traces.slice(0, 2).entries()) {
        const head = ((time * (index ? 70 : 110) + index * width * .55) % (width + 400)) - 200;
        for (let i = 0; i < 26; i++) {
          const x = head - i * 7;
          context!.globalAlpha = (trace.alpha + .22) * (1 - i / 26) ** 2;
          context!.beginPath(); context!.arc(x, y(trace, x), i ? 1.1 : 2, 0, Math.PI * 2); context!.fill();
        }
      }
      // Slow scanning sweep.
      const sweep = (time * 34) % (width + 600) - 300, gradient = context!.createLinearGradient(sweep - 160, 0, sweep, 0);
      gradient.addColorStop(0, 'transparent'); gradient.addColorStop(1, color);
      context!.globalAlpha = .035; context!.fillStyle = gradient; context!.fillRect(sweep - 160, 0, 160, height);
      context!.globalAlpha = 1;
    }
    const loop = (now: number) => {
      frame = requestAnimationFrame(loop);
      const dt = Math.min(.1, (now - (previous || now)) / 1000); previous = now; time += dt;
      const present = pointer.tx > -9999, follow = 1 - Math.exp(-dt * 7);
      if (present && pointer.x <= -9999) { pointer.x = pointer.tx; pointer.y = pointer.ty; }
      if (present) { pointer.x += (pointer.tx - pointer.x) * follow; pointer.y += (pointer.ty - pointer.y) * follow; }
      pointer.strength += ((present ? 1 : 0) - pointer.strength) * (1 - Math.exp(-dt * 3));
      draw();
    };
    const running = () => active && !document.hidden && !reduced();
    const sync = () => { cancelAnimationFrame(frame); frame = 0; previous = 0; readColor(); if (running()) frame = requestAnimationFrame(loop); else draw(); };
    const move = (e: PointerEvent) => { if (e.pointerType !== 'touch') { pointer.tx = e.clientX; pointer.ty = e.clientY; } };
    const leave = () => { pointer.tx = pointer.ty = -9999; };
    const themeWatch = new MutationObserver(sync);
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'data-motion', 'data-theme', 'class'] });
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    resize(); sync();
    window.addEventListener('resize', resize); document.addEventListener('visibilitychange', sync); media.addEventListener('change', sync);
    window.addEventListener('pointermove', move, { passive: true }); document.documentElement.addEventListener('pointerleave', leave);
    return () => {
      cancelAnimationFrame(frame); themeWatch.disconnect();
      window.removeEventListener('resize', resize); document.removeEventListener('visibilitychange', sync); media.removeEventListener('change', sync);
      window.removeEventListener('pointermove', move); document.documentElement.removeEventListener('pointerleave', leave);
    };
  }, [active]);
  return <canvas ref={ref} className="ambient-field" aria-hidden="true"/>;
}
