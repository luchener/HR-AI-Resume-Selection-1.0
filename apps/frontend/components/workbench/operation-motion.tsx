'use client';

import { useCallback, useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { CheckIcon, CircleAlertIcon, XIcon } from 'lucide-react';
import styles from './operation-motion.module.css';

export type OperationPhase = 'pending' | 'success' | 'error';

export function useAnimationCompletion<T extends Element>(active: boolean, ref: RefObject<T | null>, onEnd: () => void) {
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    const animations = ref.current?.getAnimations?.() ?? [];
    void Promise.allSettled(animations.map((animation) => animation.finished)).then(() => {
      if (!cancelled) onEnd();
    });
    return () => { cancelled = true; };
  }, [active, ref, onEnd]);
}

export function useMotionSequence(open: boolean, outcome: OperationPhase) {
  const introRef = useRef<SVGGElement>(null);
  const finishRef = useRef<SVGGElement>(null);
  const [introDone, setIntroDone] = useState(false);
  const [finished, setFinished] = useState(false);
  const markIntroDone = useCallback(() => setIntroDone(true), []);
  const markFinished = useCallback(() => setFinished(true), []);
  const motionPhase = outcome === 'success' && !introDone ? 'pending' : outcome;

  useEffect(() => {
    if (!open) {
      setIntroDone(false);
      setFinished(false);
    }
  }, [open]);
  useAnimationCompletion(open, introRef, markIntroDone);
  useAnimationCompletion(open && motionPhase === 'success', finishRef, markFinished);

  const statusPhase = motionPhase === 'success' && !finished ? 'pending' : motionPhase;
  return { motionPhase, statusPhase, introRef, finishRef };
}

export function OperationFrame({ title, panelRef, closeRef, id, eyebrow, phase, status, detail, onClose, closeLabel = '关闭', graphic, action, morph = false }: {
  title: string;
  panelRef: RefObject<HTMLDivElement | null>;
  closeRef: RefObject<HTMLButtonElement | null>;
  id: string;
  eyebrow: string;
  phase: OperationPhase;
  status: string;
  detail: string;
  onClose: () => void;
  closeLabel?: string;
  graphic: ReactNode;
  action?: ReactNode;
  morph?: boolean;
}) {
  return (
    <div ref={panelRef} className={`${styles.panel} ${morph ? '' : styles.motionPanel}`} role="dialog" aria-modal="true" aria-labelledby={id}>
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>{eyebrow}</p><h2 id={id}>{title}</h2></div>
        <button ref={closeRef} type="button" onClick={onClose} className={styles.close} aria-label={closeLabel}>
          <XIcon size={18} strokeWidth={1.5} />
        </button>
      </header>
      <div className={styles.stage} aria-hidden="true">{graphic}</div>
      <footer className={styles.footer}>
        <div className={styles.status} data-phase={phase} role="status">
          <span className={styles.statusMark} aria-hidden="true">
            {phase === 'success' ? <CheckIcon size={15} /> : phase === 'error' ? <CircleAlertIcon size={15} /> : <span />}
          </span>
          <div><p className={styles.statusTitle}>{status}</p><p className={styles.detail}>{detail}</p></div>
        </div>
        <div className={styles.footerAction}>{action}</div>
      </footer>
    </div>
  );
}

const folderLayers = [
  { x: 120, y: 143, face: '#a34a3b', tab: '#ca6750', tabX: 12 },
  { x: 134, y: 157, face: '#ebe6d8', tab: '#faf8f0', tabX: 102 },
  { x: 148, y: 171, face: '#b8bfaf', tab: '#d6ddcf', tabX: 12 },
  { x: 162, y: 186, face: '#d8d3c6', tab: '#f3efdf', tabX: 102 },
];

type MotionRefs = { introRef: RefObject<SVGGElement | null>; finishRef: RefObject<SVGGElement | null> };

export function ArchiveMachine({ candidate, category, phase, drawerStage, introRef, finishRef, drawerRef, departureRef }: MotionRefs & {
  candidate: string; category: string; phase: OperationPhase;
  drawerStage: 'initial' | 'closing' | 'closed' | 'reopening' | 'extracting';
  drawerRef: RefObject<SVGGElement | null>;
  departureRef: RefObject<SVGGElement | null>;
}) {
  const drawerMask = `drawer-mouth-${useId()}`;
  const short = (value: string, limit: number) => value.length > limit ? `${value.slice(0, limit)}…` : value;
  return (
    <svg className={styles.machine} viewBox="52 12 450 348" data-phase={phase} data-drawer={drawerStage} focusable="false">
      <defs><clipPath id={drawerMask}><path d="M184 177 366 136 496 242V346H184Z" /></clipPath></defs>
      <ellipse cx="275" cy="307" rx="155" ry="23" fill="#272823" opacity=".1" />
      <path d="M113 90 290 54 366 136 191 174Z" fill="#383b39" />
      <path d="M113 90 191 174V249L113 164Z" fill="#292c2b" />
      <path d="M191 174 366 136V211L191 249Z" fill="#202323" />
      <path d="M198 180 360 145V205L198 240Z" fill="#111312" />
      <path d="M115 91 191 173 363 136" fill="none" stroke="#555953" />
      <g clipPath={`url(#${drawerMask})`}><g className={styles.drawerTravel}>
        <path d="M126 153 193 233 367 195 300 118Z" fill="#252925" />
        <path d="M126 137 193 215V233L126 153Z" fill="#454842" />
        <path d="M126 138 193 215" stroke="#747870" />
        {folderLayers.map((folder) => (
          <g key={folder.x} transform={`matrix(1 -.218 0 1 ${folder.x} ${folder.y})`}>
            <rect width="164" height="53" rx="2" fill={folder.face} />
            <rect x={folder.tabX} y="-6" width="39" height="9" rx="2" fill={folder.tab} />
            <path d="M10 9h142M10 16h65" stroke="#292d28" strokeOpacity=".2" />
          </g>
        ))}
        <path d="M126 165 194 237V252L126 183Z" fill="#343833" />
        <path d="M126 165 194 237" stroke="#797d72" />
      </g></g>
      <g clipPath={drawerStage !== 'initial' && drawerStage !== 'extracting' ? `url(#${drawerMask})` : undefined}>
        <g className={styles.storedCard}>
          <g transform="translate(266 200)">
            <g ref={departureRef} className={styles.cardDeparture}>
              <g ref={introRef} className={styles.archiveApproach}>
                <g ref={finishRef} className={styles.archiveCard}>
                  <g transform="matrix(1 -.218 0 1 0 0)">
                    <path d="M2 0h10v-8h50v8h94q2 0 2 2v67H0V2q0-2 2-2" fill="#f6f4ec" stroke="#898d80" />
                    <rect x="10" y="10" width="3" height="40" fill="#277354" />
                    <text x="21" y="17" fill="#526157" fontSize="8" className={styles.machineLabel}>CANDIDATE FILE</text>
                    <text x="21" y="35" fill="#222724" fontSize="14" fontWeight="650">{short(candidate, 9)}</text>
                    <text x="21" y="49" fill="#526157" fontSize="9">{short(category, 12)}</text>
                  </g>
                </g>
              </g>
            </g>
          </g>
        </g>
      </g>
      <g ref={drawerRef} className={styles.drawerTravel}>
        <path d="M191 174 366 136 370 138 194 177Z" fill="#babcaf" />
        <path d="M194 177 370 138V214L194 252Z" fill="#777c77" stroke="#5e645f" />
        <path d="M198 181 366 144V210L198 247Z" fill="none" stroke="#aeb1a8" strokeOpacity=".6" />
        <g transform="matrix(1 -.218 0 1 236 194)">
          <rect width="92" height="20" rx="2" fill="#282c29" stroke="#939991" />
          <text x="46" y="13" textAnchor="middle" fill="#e7e9e1" fontSize="8" className={styles.machineLabel}>TALENT FILES</text>
          <rect x="23" y="31" width="46" height="5" rx="2" fill="#c5c8bd" stroke="#565c57" />
        </g>
        <circle cx="350" cy="202" r="4" className={styles.drawerLamp} />
      </g>
    </svg>
  );
}

export function PrinterMachine({ format, phase, settled, introRef, finishRef }: MotionRefs & { format: string; phase: OperationPhase; settled: boolean }) {
  const id = useId();
  const outputMask = `print-output-${id}`;
  const glassMask = `print-glass-${id}`;

  return (
    <svg className={styles.machine} viewBox="40 1 400 328" data-phase={phase} focusable="false">
      <defs>
        <clipPath id={outputMask}><rect x="125" y="224" width="230" height="91" /></clipPath>
        <clipPath id={glassMask}><rect x="117" y="115" width="246" height="71" rx="5" /></clipPath>
      </defs>
      <ellipse cx="240" cy="299" rx="132" ry="16" fill="#282722" opacity=".13" />
      <rect x="130" y="184" width="220" height="115" rx="10" fill="#242622" stroke="#343630" />
      <path d="M136 286h208v5q0 4-4 4H140q-4 0-4-4Z" fill="#30332d" />
      <g clipPath={`url(#${outputMask})`}>
        <g ref={finishRef} className={styles.printOutput}>
          <rect x="146" y="209" width="188" height="78" rx="2" fill="#f8f6ed" stroke="#dad5c6" />
          <path d="M159 239h161m-161 32h161" stroke="#d2cfc2" />
          <text x="159" y="254" fill="#232723" fontSize="13" fontWeight="650">{settled ? '报告打印完成' : '报告打印中'}</text>
          <text x="159" y="266" fill="#62695e" fontSize="8" className={styles.machineLabel}>{format} / EXPORT COMPLETE</text>
        </g>
      </g>
      <rect x="102" y="101" width="276" height="129" rx="14" fill="#1b1d1b" stroke="#0f110f" strokeWidth="2" />
      <rect x="108" y="107" width="264" height="113" rx="9" fill="#30332f" />
      <rect x="117" y="115" width="246" height="71" rx="5" fill="#151a17" stroke="#41463d" />
      <rect x="126" y="123" width="228" height="55" rx="2" fill="#242b25" />
      <path d="M128 176 218 124h60l-90 52Z" fill="#817f69" opacity=".09" />
      <path d="M120 224h240" stroke="#0d0e0c" strokeWidth="5" />
      <rect x="118" y="193" width="102" height="19" rx="3" fill="#121411" stroke="#444640" />
      <text x="126" y="206" fill="#f0a487" fontSize="9" className={styles.machineLabel}>{settled ? 'COMPLETE' : phase === 'success' ? 'EJECTING' : phase === 'error' ? 'ERROR' : 'PROCESSING'}</text>
      <path d="M242 204h60m-60 4h60" stroke="#4f5148" strokeDasharray="2 3" />
      <circle cx="348" cy="203" r="10" fill="#161713" />
      <circle cx="348" cy="203" r="7" fill="#bf513d" stroke="#e07155" />
      <g className={styles.printerSource}>
        <rect x="153" y="123" width="174" height="53" rx="2" fill="#f6f3e7" stroke="#c7c1b0" />
        <text x="164" y="143" fill="#554e42" fontSize="8" className={styles.machineLabel}>SOURCE / {format}</text>
        <text x="164" y="161" fill="#262820" fontSize="12" fontWeight="600">报告打印中</text>
        <path d="M290 145h23m-23 6h23m-23 6h17" stroke="#a29d8e" />
      </g>
      <g clipPath={`url(#${glassMask})`}>
        <g ref={introRef} className={styles.scanBeam}>
          <rect x="126" y="120" width="14" height="62" fill="#ff7d56" opacity=".26" />
          <path d="M140 120v62" stroke="#ff9c76" strokeWidth="2" />
        </g>
      </g>
      <g className={styles.printerLid}>
        <rect x="105" y="105" width="270" height="84" rx="9" fill="#282b27" stroke="#191b18" strokeWidth="3" />
        <rect x="114" y="114" width="252" height="66" rx="5" fill="none" stroke="#555850" />
        <path d="M124 126h55m-55 4h39" stroke="#5e6258" />
      </g>
    </svg>
  );
}
