import { AuthProvider } from '@/components/workbench/auth-context';
import { AnalysisProvider } from '@/components/workbench/analysis-context';

export default function DefaultLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AnalysisProvider>
        <main className="min-h-screen flex flex-col">{children}</main>
      </AnalysisProvider>
    </AuthProvider>
  );
}
