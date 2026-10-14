import type { ReactNode } from "react";

/* The app's action glyphs, drawn once: one 24-unit grid, one 2-unit round
   stroke, currentColor, hidden from screen readers (the control carries the
   label). A text glyph (✕ → ↑ ›) takes its weight and centring from
   whichever font renders it; these don't.

   Size: 1em unless `size` says otherwise, and a CSS width/height on the svg
   (`.sheet-close svg`, `.nav-back svg`) outranks both — as does a CSS
   stroke-width, so a control styled for its glyph keeps its look. A primary
   button's glyph (the send arrow, the +) may pass strokeWidth={2.4}. */

type IconProps = {
  size?: number | string;
  strokeWidth?: number;
  className?: string;
};

function Icon({
  size = "1em",
  strokeWidth = 2,
  className,
  children,
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      {children}
    </svg>
  );
}

export function Close(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Icon>
  );
}

export function ChevronLeft(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M15 5l-7 7 7 7" />
    </Icon>
  );
}

export function ChevronRight(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M9 5l7 7-7 7" />
    </Icon>
  );
}

export function ChevronDown(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M5 9l7 7 7-7" />
    </Icon>
  );
}

export function ArrowRight(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Icon>
  );
}

export function ArrowUp(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M12 19V5M6 11l6-6 6 6" />
    </Icon>
  );
}

export function Camera(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
      <circle cx="12" cy="13" r="3.5" />
    </Icon>
  );
}

export function Mic(p: IconProps) {
  return (
    <Icon {...p}>
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4" />
    </Icon>
  );
}

export function Check(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </Icon>
  );
}

export function Pencil(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M4 20l1-4L16 5a2.1 2.1 0 0 1 3 3L8 19z" />
      <path d="M14 7l3 3" />
    </Icon>
  );
}

export function Trash(p: IconProps) {
  return (
    <Icon {...p}>
      <path d="M4 7h16M10 11v6M14 11v6" />
      <path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </Icon>
  );
}
