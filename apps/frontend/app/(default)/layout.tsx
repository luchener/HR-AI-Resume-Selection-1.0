import { AuthProvider } from '@/components/workbench/auth-context';
import { AnalysisProvider } from '@/components/workbench/analysis-context';
import { AnnouncementProvider } from '@/components/workbench/announcement-modal';

export default function DefaultLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AnalysisProvider>
        <AnnouncementProvider>
          <main className="min-h-screen flex flex-col">{children}</main>
        </AnnouncementProvider>
      </AnalysisProvider>
    </AuthProvider>
  );
}
