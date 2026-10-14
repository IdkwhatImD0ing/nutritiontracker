/** A loading placeholder in the shape of what is coming (.skeleton,
 *  globals.css): `rows` blocks of `height` px, 8px apart, so the page holds
 *  its layout instead of a "Loading…" line that jumps when the data lands.
 *  The blocks are hidden from a screen reader, which hears one "Loading…"
 *  instead of n empty boxes. Spans, so it fits inside a <p> too. */
export default function Skeleton({
  rows = 1,
  height = 16,
  width,
  radius,
}: {
  rows?: number;
  height?: number;
  /** px or any CSS length; full width by default */
  width?: number | string;
  /** px; --radius-sm by default */
  radius?: number;
}) {
  return (
    <span className="skeleton-stack" aria-busy="true">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: rows }, (_, i) => (
        <span
          key={i}
          className="skeleton"
          aria-hidden="true"
          style={{ height, width, borderRadius: radius }}
        />
      ))}
    </span>
  );
}
