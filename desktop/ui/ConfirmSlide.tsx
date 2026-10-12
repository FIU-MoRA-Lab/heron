import { createSignal } from 'solid-js';
import { ArrowLeft, ArrowRight } from 'lucide-solid';

/** Intentional drag from the handle, followed by release at the far end. */
export default function ConfirmSlide(props: {
  label: string; disabled?: boolean; reverse?: boolean; onConfirm: () => void;
}) {
  const [progress, setProgress] = createSignal(0);
  let tracking = false;
  let startX = 0;
  let distance = 1;
  function reset() { tracking = false; setProgress(0); }
  return <div class="confirm-slide" classList={{ disabled: props.disabled, reverse: props.reverse }}
    role="group" aria-label={props.label}>
    <div class="slide-fill" style={{ width: `${progress() * 100}%` }} />
    <span>{progress() > 0 ? 'Slide to confirm' : props.label}</span>
    <button class="slide-handle" disabled={props.disabled} aria-label={`Drag to confirm: ${props.label}`}
      style={{ [props.reverse ? 'right' : 'left']: `calc(5px + (100% - 44px) * ${progress()})` }}
      onPointerDown={event => {
        if (props.disabled) return;
        startX = event.clientX;
        distance = Math.max(1, event.currentTarget.parentElement!.clientWidth - 44);
        tracking = true;
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        if (tracking) setProgress(Math.max(0, Math.min(1,
          (event.clientX - startX) * (props.reverse ? -1 : 1) / distance)));
      }}
      onPointerUp={event => {
        const confirmed = tracking && progress() >= 0.9 && !props.disabled;
        reset();
        event.currentTarget.releasePointerCapture(event.pointerId);
        if (confirmed) props.onConfirm();
      }}
      onPointerCancel={reset} onLostPointerCapture={reset}>
      {props.reverse ? <ArrowLeft size={18} aria-hidden="true" /> : <ArrowRight size={18} aria-hidden="true" />}
    </button>
  </div>;
}
