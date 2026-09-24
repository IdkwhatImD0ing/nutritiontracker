import { CopyButton } from "./CopyButton";

/** One code sample: a bar with a filename-style `title` (or the language when
 *  there is none), the language label and Copy, then the code. The <pre>
 *  scrolls sideways inside the block, never the page, and is focusable so a
 *  keyboard can scroll it. No highlighting library: the samples are short and
 *  plain text reads fine. */
export function CodeBlock({ code, lang, title }: { code: string; lang: string; title?: string }) {
  const name = title ?? lang;
  return (
    <div className="docs-code">
      <div className="docs-code-bar">
        <span className="docs-code-title">{name}</span>
        <span className="docs-code-end">
          {title && <span className="docs-code-lang">{lang}</span>}
          <CopyButton text={code} label={title ?? `${lang} example`} />
        </span>
      </div>
      <pre tabIndex={0} aria-label={`${name} code`}>
        <code>{code}</code>
      </pre>
    </div>
  );
}
