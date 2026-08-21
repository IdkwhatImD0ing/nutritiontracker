"use client";

import { useState } from "react";

type Props = {
  busy: boolean;
  error: string | null;
  /** Returns true on success; the field only clears then, so a failed log
   *  can be resubmitted unchanged (same client uuid upstream). */
  onSubmit: (text: string) => Promise<boolean>;
};

export default function CaptureBar({ busy, error, onSubmit }: Props) {
  const [text, setText] = useState("");

  return (
    <div>
      <form
        className="capture"
        onSubmit={async (e) => {
          e.preventDefault();
          const trimmed = text.trim();
          if (!trimmed || busy) return;
          const ok = await onSubmit(trimmed);
          if (ok) setText("");
        }}
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="What did you eat?"
          aria-label="Log food"
          enterKeyHint="send"
          autoComplete="off"
        />
        <button type="submit" disabled={busy || !text.trim()} aria-label="Log">
          {busy ? <span className="spinner" /> : "↑"}
        </button>
      </form>
      {error ? <p className="capture-error">{error}</p> : null}
    </div>
  );
}
