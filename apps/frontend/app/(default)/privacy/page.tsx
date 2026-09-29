import type { Metadata } from 'next';

import LegalDoc from '@/components/legal/legal-doc';
import { PRIVACY_POLICY } from '@/lib/legal-content';

export const metadata: Metadata = {
  title: '隐私政策 | 简历智选',
  description: '简历智选系统如何收集、使用、存储与保护你的个人信息。',
};

export default function PrivacyPage() {
  return <LegalDoc doc={PRIVACY_POLICY} />;
}
