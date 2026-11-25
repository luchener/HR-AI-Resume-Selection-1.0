export default function BrandMark({ className = '' }: { className?: string }) {
  return (
    <span className={`flex shrink-0 items-center justify-center bg-ink text-white ${className}`} aria-hidden="true">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.3} className="size-[55%]">
        <path d="M3.2 1.4h6.1l3.5 3.4v9.8H3.2z" />
        <path d="M9.2 1.4v3.5h3.5" />
        <path d="M5.4 8h5.2M5.4 10.4h5.2M5.4 12.8h3" />
      </svg>
    </span>
  );
}
