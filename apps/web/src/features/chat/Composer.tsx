import { type KeyboardEvent, useEffect, useRef, useState } from "react";

interface Props {
  busy: boolean;
  canStop: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => void;
}

export function Composer({ busy, canStop, onSend, onStop }: Props) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  async function submit() {
    const content = text.trim();
    if (!content || busy) return;
    setText("");
    try {
      await onSend(content);
    } catch {
      setText(content); // keep the draft so nothing typed is lost
    }
    ref.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <div className="rounded-2xl border border-line bg-canvas p-2 shadow-sm focus-within:border-accent/60">
      <textarea
        ref={ref}
        // Ready to type on desktop; not on touch screens, where it would pop the keyboard.
        autoFocus={window.matchMedia?.("(pointer: fine)").matches}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Message Celius…"
        aria-label="Message"
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 leading-relaxed outline-none"
      />
      <div className="flex justify-end">
        {canStop ? (
          <button
            onClick={onStop}
            className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-panel"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={() => void submit()}
            disabled={!text.trim() || busy}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
