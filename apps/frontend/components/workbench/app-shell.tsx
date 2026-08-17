import type { ReactNode } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { BarChart3Icon, LayoutDashboardIcon } from 'lucide-react';

type AppShellProps = {
  active: 'home' | 'report';
  children: ReactNode;
};

const navigation = [
  { id: 'home' as const, label: '首页控制台', href: '/', icon: LayoutDashboardIcon },
  { id: 'report' as const, label: '分析报告页', href: '/dashboard', icon: BarChart3Icon },
];

export default function AppShell({ active, children }: AppShellProps) {
  return (
    <div className="min-h-screen bg-[#f3f6fa] lg:grid lg:grid-cols-[272px_minmax(0,1fr)]">
      <aside className="relative overflow-hidden bg-[#111c31] px-5 py-5 text-white lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:px-7 lg:py-8">
        <div className="flex items-center gap-3">
          <Image
            src="/brand/resume-screening-logo.svg"
            alt=""
            width={44}
            height={44}
            className="size-11 shrink-0"
            aria-hidden="true"
          />
          <div>
            <p className="text-lg font-semibold">AI 简历智选</p>
            <p className="text-xs text-slate-400">Recruiting workspace</p>
          </div>
        </div>

        <div className="mt-8 hidden lg:block">
          <p className="text-xs font-semibold uppercase text-[#78a0ff]">Screening flow</p>
          <p className="mt-3 text-2xl font-semibold leading-9 text-white">💼</p>
        </div>

        <nav className="mt-6 grid grid-cols-2 gap-2 lg:mt-12 lg:grid-cols-1" aria-label="页面导航">
          {navigation.map((item, index) => {
            const Icon = item.icon;
            const isActive = item.id === active;
            return (
              <Link
                key={item.id}
                href={item.href}
                aria-current={isActive ? 'page' : undefined}
                className={`flex min-w-0 flex-col items-center gap-1 rounded-md border px-1 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#78a0ff] lg:flex-row lg:gap-3 lg:px-3 lg:py-3 ${
                  isActive
                    ? 'border-[#78a0ff]/50 bg-[#1b2a47]'
                    : 'border-transparent text-slate-400 hover:border-white/10 hover:bg-white/5 hover:text-slate-200'
                }`}
              >
                <span className={`flex size-8 shrink-0 items-center justify-center rounded-md ${isActive ? 'bg-[#78a0ff] text-[#11203b]' : 'bg-white/5'}`}>
                  <Icon className="size-4" aria-hidden="true" />
                </span>
                <span className="min-w-0 text-center lg:text-left">
                  <span className="hidden text-[11px] text-slate-500 lg:block">0{index + 1}</span>
                  <span className={`block truncate text-xs font-medium lg:text-sm ${isActive ? 'text-white' : ''}`}>{item.label}</span>
                </span>
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto hidden rounded-md border border-white/10 bg-white/5 p-4 lg:block">
          <div className="flex items-center gap-2 text-xs font-medium">
            <span className="size-2 rounded-full bg-emerald-400" />
            ©Ai简历智选 1.0<br />Develop By WickLu<br />Mail：luchenstudio@163.com
          </div>

        </div>
      </aside>
      <main className="min-w-0">{children}</main>
    </div>
  );
}
