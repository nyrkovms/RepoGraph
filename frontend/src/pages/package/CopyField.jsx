import { useEffect, useRef, useState } from "react";

// Selects the text of an element, so a failed copy leaves it ready for Ctrl+C.
function selectText(element) {
  const selection = window.getSelection?.();
  if (!element || !selection) {
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
}

// `label` names the field for the button's accessible name; the visible label is the fact's own term above it.
export function CopyField({ label, text }) {
  const [state, setState] = useState("idle");
  const textRef = useRef(null);
  const timer = useRef(0);

  useEffect(() => () => clearTimeout(timer.current), []);

  function flash(next) {
    setState(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2000);
  }

  // The call stays inside the click handler: browsers allow the clipboard only for a user gesture.
  function copy() {
    let pending;
    try {
      pending = Promise.resolve(navigator.clipboard.writeText(text));
    } catch (error) {
      pending = Promise.reject(error);
    }
    pending.then(
      () => flash("copied"),
      () => {
        selectText(textRef.current);
        flash("failed");
      }
    );
  }

  return (
    <div className="copy-field">
      <code ref={textRef}>{text}</code>
      <button type="button" className="copy-button" onClick={copy} aria-label={`Copy ${label}`}>
        Copy
      </button>
      <span className="muted copy-field-state" role="status">
        {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : ""}
      </span>
    </div>
  );
}
