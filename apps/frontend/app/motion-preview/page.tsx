import { Suspense } from 'react';
import { notFound } from 'next/navigation';
import MotionPreview from './preview';

export default function MotionPreviewPage() {
  if (process.env.NODE_ENV !== 'development') notFound();
  return <Suspense fallback={null}><MotionPreview /></Suspense>;
}
