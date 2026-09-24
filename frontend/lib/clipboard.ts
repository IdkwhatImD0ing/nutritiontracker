/** Copy `text` to the clipboard, or throw. The async Clipboard API needs a
 *  secure context and can still be refused (permissions, an embedded frame);
 *  a selected hidden textarea plus execCommand("copy") is the fallback that
 *  works in those places. */
export async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the legacy path
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  // select() moves focus into the textarea and remove() drops it to <body>;
  // hand it back so a keyboard user is still on the button they pressed.
  const prior = document.activeElement;
  area.select();
  area.setSelectionRange(0, text.length); // iOS Safari ignores select() alone
  const ok = document.execCommand("copy");
  area.remove();
  if (prior instanceof HTMLElement) prior.focus({ preventScroll: true });
  if (!ok) throw new Error("copy refused");
}
