import type { Metadata } from 'next';

import LegalDoc from '@/components/legal/legal-doc';
import { USER_AGREEMENT } from '@/lib/legal-content';

export const metadata: Metadata = {
  title: '用户协议 | 简历智选',
  description: '简历智选系统用户协议：服务内容、账号规则、上传数据的合法性保证与责任划分。',
};

export default function TermsPage() {
  return <LegalDoc doc={USER_AGREEMENT} />;
}
