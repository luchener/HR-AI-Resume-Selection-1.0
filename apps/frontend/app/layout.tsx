import type { Metadata, Viewport } from 'next';
import { Geist, Space_Grotesk } from 'next/font/google';
import './(default)/css/globals.css';

const spaceGrotesk = Space_Grotesk({
  variable: '--font-space-grotesk',
  subsets: ['latin'],
  display: 'swap',
});

const geist = Geist({
  variable: '--font-geist',
  subsets: ['latin'],
  display: 'swap',
});

export const metadata: Metadata = {
  metadataBase: new URL('https://www.luchenstudio.cn'),
  title: '简历智选',
  description: '面向招聘场景的简历与岗位匹配分析工具',
  applicationName: '简历智选',
  keywords: ['简历筛选', '岗位匹配', '招聘分析'],
  icons: {
    icon: '/brand/resume-screening-favicon.svg',
    apple: '/brand/resume-screening-favicon.svg',
  },
  openGraph: {
    title: '简历智选 · AI 简历筛选工作台',
    description: '围绕岗位需求量化匹配简历，输出可追溯的招聘建议。',
    images: ['/brand/resume-screening-logo.png'],
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className={`${geist.variable} ${spaceGrotesk.variable} antialiased`}>
        {children}
      </body>
    </html>
  );
}
