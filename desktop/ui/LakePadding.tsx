import { createEffect, createSignal, onCleanup, untrack } from 'solid-js';
import type { SendCommand } from './types';

export default function LakePadding(props: {
  value: number; enabled: boolean; command: SendCommand;
  exportEnabled: boolean; onExport: () => void;
}) {
  const [draft, setDraft] = createSignal<number>();
  const [sending, setSending] = createSignal(false);
  let pending: number | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let acknowledgement: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  const value = () => draft() ?? props.value;

  function reset() {
    clearTimeout(timer); timer = undefined;
    clearTimeout(acknowledgement);
    pending = undefined;
    setDraft(undefined);
  }

  async function flush() {
    clearTimeout(timer); timer = undefined;
    if (disposed || sending() || pending === undefined || !props.enabled) return;
    const meters = pending;
    pending = undefined;
    setSending(true);
    try { await props.command('padding', { meters }); }
    finally {
      if (!disposed) {
        setSending(false);
        if (pending !== undefined) schedule();
        else {
          // Keep the optimistic value until polling acknowledges it. If a
          // command fails, fall back to the authoritative backend value.
          clearTimeout(acknowledgement);
          acknowledgement = setTimeout(() => {
            if (!sending() && pending === undefined) setDraft(undefined);
          }, 1500);
        }
      }
    }
  }

  function schedule() {
    if (!timer) timer = setTimeout(() => { void flush(); }, 75);
  }

  function input(meters: number) {
    if (!props.enabled) return;
    clearTimeout(acknowledgement);
    setDraft(meters);
    pending = meters;
    schedule();
  }

  createEffect(() => {
    const enabled = props.enabled;
    const current = props.value;
    const inFlight = sending();
    if (!enabled) reset();
    else if (!inFlight && pending === undefined && untrack(draft) === current) {
      clearTimeout(acknowledgement);
      setDraft(undefined);
    }
  });

  onCleanup(() => { disposed = true; reset(); });

  return <div class="lake-controls glass">
    <div class="eyebrow">Lake safety padding <strong>{value().toFixed(1)} m</strong></div>
    <input aria-label="Lake safety padding in meters" type="range" min="0" max="10" step="0.1"
      disabled={!props.enabled} value={value()}
      onInput={event => input(Number(event.currentTarget.value))}
      onChange={() => { void flush(); }} />
    <small>{props.enabled ? 'Inset shore · fountain radius' : 'Locked during missions / armed'}</small>
    <button class="lake-export" disabled={!props.exportEnabled} onClick={props.onExport}>Export lake</button>
  </div>;
}
