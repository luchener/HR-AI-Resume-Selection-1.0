import Link from 'next/link';
import type { LegalDocument } from '@/lib/legal-content';
import { CONTACT_EMAIL, OPERATOR_NAME, OPERATOR_SITE, SERVICE_NAME } from '@/lib/legal-content';

/** 极简行内加粗：把 **文字** 渲染为强调（法务文案只用到这一种标记） */
function Inline({ text }: { text: string }) {
  const parts = text.split(/\*\*(.+?)\*\*/g);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <strong key={index} className="font-semibold text-ink">
            {part}
          </strong>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

export default function LegalDoc({
  doc,
  backHref = '/',
  backLabel = '返回工作台',
}: {
  doc: LegalDocument;
  backHref?: string;
  backLabel?: string;
}) {
  return (
    <div className="min-h-screen bg-soft">
      <div className="mx-auto w-full max-w-3xl px-5 py-8 sm:px-8 lg:py-12">
        {/* 顶部 */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Link href={backHref} className="text-sm font-medium text-brand hover:underline">
            ← {backLabel}
          </Link>
          <p className="text-xs text-sub">
            版本 {doc.version} · 更新于 {doc.updated} · 生效于 {doc.effective}
          </p>
        </div>

        <header className="mt-6 border-b border-line pb-6">
          <p className="text-xs font-semibold uppercase text-sub">{SERVICE_NAME}</p>
          <h1 className="mt-2 text-3xl font-semibold text-ink">{doc.title}</h1>
          <p className="mt-3 text-sm text-sub">
            {OPERATOR_NAME} · {OPERATOR_SITE}
          </p>
        </header>

        {/* 要点速览 */}
        <section className="mt-6 rounded-lg border border-line bg-white p-5">
          <h2 className="text-sm font-semibold text-ink">要点速览</h2>
          <ul className="mt-3 space-y-2">
            {doc.summary.map((text) => (
              <li key={text} className="flex gap-2 text-sm leading-6 text-body">
                <span aria-hidden className="mt-2.5 size-1.5 shrink-0 rounded-full bg-brand" />
                <span>
                  <Inline text={text} />
                </span>
              </li>
            ))}
          </ul>
        </section>

        {/* 目录 */}
        <nav aria-label="目录" className="mt-6 rounded-lg border border-line bg-white p-5">
          <h2 className="text-sm font-semibold text-ink">目录</h2>
          <ol className="mt-3 grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            {doc.sections.map((section, index) => (
              <li key={section.title}>
                <a href={`#s-${index + 1}`} className="text-sm text-body hover:text-brand hover:underline">
                  <span className="text-sub">{index + 1}.</span> {section.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        {/* 正文 */}
        <div className="mt-6 space-y-4">
          {doc.sections.map((section, index) => (
            <section
              key={section.title}
              id={`s-${index + 1}`}
              className="scroll-mt-6 rounded-lg border border-line bg-white p-5 sm:p-6"
            >
              <h2 className="flex items-baseline gap-2 text-base font-semibold text-ink">
                <span className="inline-flex size-6 shrink-0 items-center justify-center rounded bg-brand-soft text-xs font-bold text-brand-deep">
                  {index + 1}
                </span>
                <span>{section.title}</span>
              </h2>

              {section.paragraphs?.map((text) => (
                <p key={text} className="mt-3 text-sm leading-7 text-body">
                  <Inline text={text} />
                </p>
              ))}

              {section.items && (
                <ul className="mt-3 space-y-2">
                  {section.items.map((text) => (
                    <li key={text} className="flex gap-2 text-sm leading-7 text-body">
                      <span aria-hidden className="mt-3 size-1.5 shrink-0 rounded-full bg-line" />
                      <span>
                        <Inline text={text} />
                      </span>
                    </li>
                  ))}
                </ul>
              )}

              {section.table && (
                <div className="mt-4 overflow-x-auto rounded-md border border-line-soft">
                  <table className="w-full min-w-[520px] border-collapse text-left">
                    <thead>
                      <tr className="bg-mist">
                        {section.table.head.map((cell) => (
                          <th key={cell} scope="col" className="px-3 py-2 text-xs font-semibold text-sub">
                            {cell}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {section.table.rows.map((row) => (
                        <tr key={row.join('|')} className="border-t border-line-soft align-top">
                          {row.map((cell, cellIndex) => (
                            <td
                              key={cell + String(cellIndex)}
                              className={`px-3 py-2.5 text-sm leading-6 ${cellIndex === 0 ? 'font-medium text-ink' : 'text-body'}`}
                            >
                              <Inline text={cell} />
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {section.note && (
                <p className="mt-4 rounded-md border border-warn-border bg-warn-panel px-3 py-2.5 text-sm leading-6 text-warn-ink">
                  <Inline text={section.note} />
                </p>
              )}
            </section>
          ))}
        </div>

        {/* 底部联系方式 */}
        <footer className="mt-6 rounded-lg border border-line bg-white p-5">
          <p className="text-sm text-body">
            对本文件有疑问，或需要行使你的个人信息权利，请联系：
            <a href={`mailto:${CONTACT_EMAIL}`} className="ml-1 font-medium text-brand hover:underline">
              {CONTACT_EMAIL}
            </a>
          </p>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            <Link href="/privacy" className="text-body hover:text-brand hover:underline">
              隐私政策
            </Link>
            <Link href="/terms" className="text-body hover:text-brand hover:underline">
              用户协议
            </Link>
            <Link href={backHref} className="text-body hover:text-brand hover:underline">
              {backLabel}
            </Link>
          </div>
          <p className="mt-4 text-xs leading-6 text-sub">
            {OPERATOR_NAME} · {OPERATOR_SITE} · {doc.version} · {doc.updated}
          </p>
        </footer>
      </div>
    </div>
  );
}
