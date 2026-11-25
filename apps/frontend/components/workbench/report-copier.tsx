'use client';

import { useId, useRef } from 'react';
import { useDialogBehavior } from './dialog-behavior';
import { OperationFrame, PrinterMachine, useMotionSequence } from './operation-motion';
import styles from './operation-motion.module.css';

const FORMAT_LABEL = { pdf: 'PDF', png: 'JPG', doc: 'DOC' };

export default function ReportCopier({ open, format, exporting, error, completed, onClose, preview = false }: {
  open: boolean;
  format: keyof typeof FORMAT_LABEL;
  exporting: boolean;
  error: string;
  completed: boolean;
  onClose: () => void;
  preview?: boolean;
}) {
  const id = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const { motionPhase, statusPhase: phase, introRef, finishRef } = useMotionSequence(open, error ? 'error' : completed ? 'success' : 'pending');

  useDialogBehavior({ open, onClose, panelRef, initialFocusRef: closeRef });
  if (!open) return null;

  const formatLabel = FORMAT_LABEL[format];
  const closeLabel = exporting ? '取消导出' : '关闭';

  return (
    <div className={styles.overlay} onClick={(event) => { if (!exporting && event.target === event.currentTarget) onClose(); }}>
      <OperationFrame
        id={id}
        panelRef={panelRef}
        closeRef={closeRef}
        title="导出报告"
        eyebrow={`REPORT EXPORT / ${formatLabel}`}
        phase={phase}
        status={phase === 'success' ? '报告打印完成' : phase === 'error' ? '导出失败' : '报告打印中'}
        detail={error || (phase === 'success'
          ? (preview ? `${formatLabel} 演示完成，未下载文件。` : `${formatLabel} 文件已生成，已发起下载。`)
          : `正在准备 ${formatLabel} 文件。`)}
        onClose={onClose}
        closeLabel={exporting ? closeLabel : '关闭导出窗口'}
        graphic={<PrinterMachine format={formatLabel} phase={motionPhase} settled={phase === 'success'} introRef={introRef} finishRef={finishRef} />}
        action={<button type="button" onClick={onClose} className={styles.button}>{closeLabel}</button>}
      />
    </div>
  );
}
