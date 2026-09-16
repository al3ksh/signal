import { useEffect, useRef } from 'react';

export function AnimatedNumber({ value, digits = 1, enabled = true }: { value: number | null | undefined; digits?: number; enabled?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const previous = useRef(value);
  useEffect(() => {
    const target = ref.current;
    if (!target) return;
    const format = (n: number) => n.toLocaleString('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    if (value == null) { target.textContent = '···'; previous.current = value; return; }
    const start = previous.current;
    if (!enabled || start == null || start === value || document.hidden) { target.textContent = format(value); previous.current = value; return; }
    const beginning = performance.now();
    let frame = 0;
    function tick(now: number) {
      const progress = Math.min(1, (now - beginning) / 480);
      const current = start! + (value! - start!) * (1 - (1 - progress) ** 3);
      if (target) target.textContent = format(current);
      previous.current = current;
      if (progress < 1) frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value, digits, enabled]);
  return <span ref={ref}>{value == null ? '···' : value.toLocaleString('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits })}</span>;
}

export function useRowMotion(key: string, enabled: boolean) {
  const ref = useRef<HTMLTableSectionElement>(null);
  const positions = useRef(new Map<string, number>());
  useEffect(() => {
    const rows = Array.from(ref.current?.querySelectorAll<HTMLTableRowElement>('tr[data-id]') || []);
    const next = new Map<string, number>();
    for (const row of rows) {
      const id = row.dataset.id!;
      const y = row.offsetTop;
      const old = positions.current.get(id);
      next.set(id, y);
      if (enabled && old != null && Math.abs(old - y) > 1) row.animate([{ transform: `translateY(${old - y}px)` }, { transform: 'translateY(0)' }], { duration: 320, easing: 'cubic-bezier(.16,1,.3,1)' });
    }
    positions.current = next;
  }, [key, enabled]);
  return ref;
}
