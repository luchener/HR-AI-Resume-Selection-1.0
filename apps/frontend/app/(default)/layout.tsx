import { AnalysisProvider } from '@/components/workbench/analysis-context';
import { AiModelProvider } from '@/components/workbench/ai-model-config';

export default function DefaultLayout({ children }: { children: React.ReactNode }) {
  return (
    <AiModelProvider>
      <AnalysisProvider>
        <main className="min-h-screen flex flex-col">{children}</main>
      </AnalysisProvider>
    </AiModelProvider>
  );
}
