/** The app icon's tally mark (public/icons/icon.svg), redrawn on an accent
 *  tile so it still reads at 22-26px. Shared by the landing and docs headers
 *  and the auth screens; each page sizes and colours it with its own class. */
export default function TallyMark({ className }: { className: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" />
      <path d="M9.9 8.5v15M14 8.5v15M18 8.5v15M22.1 8.5v15M7.6 20.1l16.8-8.2" />
    </svg>
  );
}
