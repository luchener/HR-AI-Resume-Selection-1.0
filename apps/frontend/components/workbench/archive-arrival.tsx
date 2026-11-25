'use client';

import { useEffect, useRef } from 'react';
import { useSearchParams } from 'next/navigation';
import styles from './operation-motion.module.css';

export const ARCHIVE_ARRIVAL_PARAM = 'arrival';

export function archiveRowId(archiveId: string) {
  return `archive-row-${archiveId}`;
}

export function ArchiveArrival({ ready }: { ready: boolean }) {
  const arrival = useSearchParams().get(ARCHIVE_ARRIVAL_PARAM);
  const shown = useRef<string | null>(null);

  useEffect(() => {
    if (shown.current !== arrival) shown.current = null;
    if (!ready || !arrival || shown.current === arrival) return;

    let stopHighlight: (() => void) | undefined;
    const frame = window.requestAnimationFrame(() => {
      const row = document.getElementById(archiveRowId(arrival));
      if (!row) return;

      shown.current = arrival;
      row.classList.add(styles.arrivingRow);
      const file = row.querySelector('[data-archive-file]');
      file?.classList.add(styles.arrivalFile);
      row.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
      if (document.activeElement === document.body) row.focus({ preventScroll: true });

      const clearHighlight = () => {
        row.classList.remove(styles.arrivingRow);
        file?.classList.remove(styles.arrivalFile);
      };
      const timeout = window.setTimeout(clearHighlight, 2700);
      stopHighlight = () => {
        window.clearTimeout(timeout);
        clearHighlight();
      };
    });
    return () => {
      window.cancelAnimationFrame(frame);
      stopHighlight?.();
    };
  }, [arrival, ready]);

  return null;
}
