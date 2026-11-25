'use client';

import { useEffect, useRef } from 'react';

/**
 * 登录页的跟手吉祥物：两只眼睛跟随光标，随机眨眼。
 * 纯装饰：aria-hidden，不承载信息；prefers-reduced-motion 下静止。
 */
export default function EyeCube({ className = '' }: { className?: string }) {
  const cubeRef = useRef<HTMLDivElement>(null);
  const eyeRefs = useRef<Array<HTMLSpanElement | null>>([]);

  useEffect(() => {
    const cube = cubeRef.current;
    const eyes = eyeRefs.current.filter((eye): eye is HTMLSpanElement => Boolean(eye));
    if (!cube || eyes.length === 0) return;

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const bases = eyes.map((eye) => ({
      x: eye.offsetLeft + eye.offsetWidth / 2,
      y: eye.offsetTop + eye.offsetHeight / 2,
    }));
    const state = eyes.map(() => ({ x: 0, y: 0, tx: 0, ty: 0 }));
    const maxShift = 5.5;
    let frame = 0;

    const loop = () => {
      let moving = false;
      eyes.forEach((eye, index) => {
        const s = state[index];
        s.x += (s.tx - s.x) * 0.2;
        s.y += (s.ty - s.y) * 0.2;
        if (Math.abs(s.tx - s.x) > 0.05 || Math.abs(s.ty - s.y) > 0.05) moving = true;
        eye.style.setProperty('--ex', `${s.x.toFixed(2)}px`);
        eye.style.setProperty('--ey', `${s.y.toFixed(2)}px`);
      });
      frame = moving ? window.requestAnimationFrame(loop) : 0;
    };

    const aimAt = (clientX: number, clientY: number) => {
      const rect = cube.getBoundingClientRect();
      const pointX = clientX - rect.left;
      const pointY = clientY - rect.top;

      eyes.forEach((eye, index) => {
        const dx = pointX - bases[index].x;
        const dy = pointY - bases[index].y;
        const length = Math.hypot(dx, dy) || 1;
        const pull = Math.min(length, 180) / 180;
        state[index].tx = (dx / length) * maxShift * pull;
        state[index].ty = (dy / length) * maxShift * pull;
      });

      if (!frame) frame = window.requestAnimationFrame(loop);
    };

    const rect = cube.getBoundingClientRect();
    aimAt(rect.right + 260, rect.top + rect.height * 0.55);

    let blinkTimer = 0;
    const blink = () => {
      eyes.forEach((eye) => eye.classList.add('is-blinking'));
      window.setTimeout(() => eyes.forEach((eye) => eye.classList.remove('is-blinking')), 150);
      blinkTimer = window.setTimeout(blink, 2600 + Math.random() * 3600);
    };

    const onPointerMove = (event: PointerEvent) => aimAt(event.clientX, event.clientY);

    if (!reduced) {
      window.addEventListener('pointermove', onPointerMove, { passive: true });
      blinkTimer = window.setTimeout(blink, 2400);
    }

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.clearTimeout(blinkTimer);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div ref={cubeRef} className={`eye-cube ${className}`} aria-hidden="true">
      {[0, 1].map((index) => (
        <span
          key={index}
          ref={(element) => {
            eyeRefs.current[index] = element;
          }}
          className="eye-cube__eye"
        >
          <span className="eye-cube__dot" />
        </span>
      ))}
    </div>
  );
}
