import { createEffect, createMemo, createSignal, For, onCleanup, Show } from 'solid-js';
import { invoke } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';
import { Database, Layers, SlidersHorizontal, Upload, X, Play, Pause, RotateCcw, LocateFixed } from 'lucide-solid';
import { PathLayer, ScatterplotLayer } from '@deck.gl/layers';
import type { Layer } from '@deck.gl/core';

export interface Reading {
  time: number; position: [number, number]; values: Record<string, number>;
}
interface Field { name: string; min: number; max: number; unit: string }
interface Dataset { topic: string; count: number; unaligned: number; fields: Field[]; points: Reading[] }
export interface Recording {
  filename: string; messageCount: number; duration: number; startTime: string;
  synthetic: boolean; alignment: string; unaligned: number;
  track: { time: number; position: [number, number] }[]; datasets: Dataset[];
}
const label = (value: string) => value.replaceAll('_', ' ');
const timeLabel = (value: number) => `${Math.floor(value / 60)}:${Math.floor(value % 60).toString().padStart(2, '0')}`;

export default function DataExplorer(props: {
  open: boolean; ready: boolean; onClose: () => void; beforeLoad: () => void;
  onLayers: (layers: Layer[]) => void; onFit: (recording: Recording) => void;
}) {
  const [tab, setTab] = createSignal<'data' | 'layers' | 'filters'>('data');
  const [recording, setRecording] = createSignal<Recording>();
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [topic, setTopic] = createSignal('');
  const [fieldName, setFieldName] = createSignal('');
  const [minimum, setMinimum] = createSignal(0);
  const [maximum, setMaximum] = createSignal(1);
  const [radius, setRadius] = createSignal(3);
  const [opacity, setOpacity] = createSignal(0.9);
  const [pointsVisible, setPointsVisible] = createSignal(true);
  const [trackVisible, setTrackVisible] = createSignal(true);
  const [elapsed, setElapsed] = createSignal(0);
  const [playing, setPlaying] = createSignal(false);
  const [speed, setSpeed] = createSignal(1);
  let disposed = false;
  let panel!: HTMLDivElement;
  const dataset = createMemo(() => recording()?.datasets.find(data => data.topic === topic()));
  const field = createMemo(() => dataset()?.fields.find(item => item.name === fieldName()));
  const points = createMemo(() => dataset()?.points.filter(point => {
    const value = point.values[fieldName()];
    return Number.isFinite(value) && value >= minimum() && value <= maximum();
  }) ?? []);
  const visibleCount = createMemo(() => points().filter(point => point.time <= elapsed()).length);
  const paths = createMemo(() => {
    const segments: [number, number][][] = [];
    let segment: [number, number][] = [];
    const track = recording()?.track ?? [];
    track.forEach((point, index) => {
      if (index > 0 && point.time - track[index - 1].time > 2) {
        if (segment.length > 1) segments.push(segment);
        segment = [];
      }
      segment.push(point.position);
    });
    if (segment.length > 1) segments.push(segment);
    return segments.map(path => ({ path }));
  });
  const playhead = createMemo(() => {
    const track = recording()?.track;
    if (!track?.length) return [];
    const time = elapsed();
    let low = 0, high = track.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high + 1) / 2);
      if (track[middle].time <= time) low = middle; else high = middle - 1;
    }
    const a = track[low], b = track[Math.min(low + 1, track.length - 1)];
    if (b.time - a.time > 2 && time > a.time) return [];
    const fraction = b.time > a.time ? Math.max(0, Math.min(1, (time - a.time) / (b.time - a.time))) : 0;
    return [{ position: [a.position[0] + (b.position[0] - a.position[0]) * fraction,
                         a.position[1] + (b.position[1] - a.position[1]) * fraction] as [number, number] }];
  });

  function selectField(name: string, data = dataset()) {
    setFieldName(name);
    const next = data?.fields.find(item => item.name === name);
    setMinimum(next?.min ?? 0); setMaximum(next?.max ?? 1);
  }
  function selectTopic(name: string) {
    const data = recording()?.datasets.find(item => item.topic === name);
    setTopic(name); selectField(data?.fields[0]?.name ?? '', data);
  }
  async function load() {
    props.beforeLoad();
    setPlaying(false); setError(''); setLoading(true);
    try {
      const filename = await open({ multiple: false, filters: [{ name: 'MCAP recordings', extensions: ['mcap'] }] });
      if (typeof filename !== 'string' || disposed) return;
      const result = await invoke<Recording>('backend_request', { action: 'load_mcap', data: { path: filename } });
      if (disposed) return;
      setRecording(result); selectTopic(result.datasets[0]?.topic ?? '');
      setElapsed(result.duration); setPointsVisible(true); setTrackVisible(true);
      setTab('layers'); props.onFit(result);
    } catch (reason) { if (!disposed) setError(String(reason)); }
    finally { if (!disposed) setLoading(false); }
  }
  createEffect(() => {
    if (!props.open) setPlaying(false);
    else queueMicrotask(() => panel?.focus());
  });
  createEffect(() => {
    if (!props.ready) return;
    const current = recording();
    if (!props.open || !current) { props.onLayers([]); return; }
    const selected = field();
    const time = elapsed();
    const lower = selected?.min ?? 0, upper = selected?.max ?? 1;
    const color = (point: Reading): [number, number, number, number] => {
      const value = point.values[fieldName()];
      const fraction = upper > lower ? Math.max(0, Math.min(1, (value - lower) / (upper - lower))) : 0.5;
      return [Math.round(240 - 180 * fraction), Math.round(85 + 110 * fraction), Math.round(60 + 170 * fraction), 255];
    };
    const pointRadius = radius();
    props.onLayers([
      new PathLayer({ id: 'explorer-track', data: paths(), visible: trackVisible(),
        getPath: item => item.path, getColor: [185, 205, 221, 150], getWidth: 1.5,
        widthUnits: 'pixels', jointRounded: true, capRounded: true }),
      new ScatterplotLayer<Reading>({ id: 'explorer-readings', data: points(),
        visible: pointsVisible(), getPosition: point => point.position, radiusUnits: 'pixels',
        getRadius: point => point.time <= time ? pointRadius : 0,
        getFillColor: color, opacity: opacity(), pickable: true,
        updateTriggers: { getRadius: [time, pointRadius], getFillColor: [fieldName(), lower, upper] } }),
      new ScatterplotLayer({ id: 'explorer-playhead', data: playhead(),
        getPosition: point => point.position, radiusUnits: 'pixels', getRadius: 6,
        getFillColor: [255, 255, 255], stroked: true, getLineColor: [35, 35, 35],
        lineWidthUnits: 'pixels', getLineWidth: 2 }),
    ]);
  });
  createEffect(() => {
    if (!playing()) return;
    let last = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      const end = recording()?.duration ?? 0;
      const next = Math.min(end, elapsed() + (now - last) / 1000 * speed());
      last = now; setElapsed(next);
      if (next >= end) setPlaying(false);
    }, 50);
    onCleanup(() => clearInterval(timer));
  });
  onCleanup(() => { disposed = true; props.onLayers([]); });

  return <Show when={props.open}>
    <div ref={panel} id="data-explorer" class="data-explorer glass" role="region" aria-label="Explore data" tabIndex={-1}
      onKeyDown={event => { if (event.key === 'Escape') props.onClose(); }}>
      <div class="explorer-heading"><div><span class="eyebrow">MAP WORKSPACE</span><h3>Explore data</h3></div>
        <button class="icon-button" aria-label="Close data explorer" onClick={props.onClose}><X size={18} /></button></div>
      <button class="explorer-load" onClick={() => void load()} disabled={loading()}>
        <Upload size={16} />{loading() ? 'Reading recording…' : 'Load MCAP'}</button>
      <Show when={error()}><p class="explorer-error" role="alert">{error()}</p></Show>
      <div class="explorer-tabs" role="tablist" aria-label="Explorer controls">
        <For each={[
          { id: 'data' as const, title: 'Data', icon: Database },
          { id: 'layers' as const, title: 'Layers', icon: Layers },
          { id: 'filters' as const, title: 'Filters', icon: SlidersHorizontal },
        ]}>{item => <button id={`explorer-tab-${item.id}`} role="tab" aria-selected={tab() === item.id}
          aria-controls="explorer-tab-content" classList={{ active: tab() === item.id }} onClick={() => setTab(item.id)}>
          <item.icon size={14} />{item.title}</button>}</For>
      </div>
      <div id="explorer-tab-content" role="tabpanel" aria-labelledby={`explorer-tab-${tab()}`} class="explorer-content">
        <Show when={recording()} fallback={<div class="explorer-empty"><Database size={30} />
          <h4>Your mission, mapped</h4><p>Load a recording to explore sensor readings along the vehicle path.</p>
          <small>Works locally without a vehicle connection.</small></div>}>
          <Show when={tab() === 'data'}>
            <div class="explorer-file"><Database size={16} /><strong>{recording()!.filename}</strong></div>
            <div class="explorer-stats"><div><b>{recording()!.messageCount.toLocaleString()}</b><small>messages</small></div>
              <div><b>{timeLabel(recording()!.duration)}</b><small>duration</small></div>
              <div><b>{recording()!.datasets.length}</b><small>streams</small></div></div>
            <Show when={recording()!.synthetic}><span class="explorer-badge">Synthetic recording</span></Show>
            <h4>Available streams</h4><For each={recording()!.datasets}>{data => <button class="explorer-stream"
              onClick={() => { selectTopic(data.topic); setTab('layers'); }}><span>{data.topic}</span><small>{data.count.toLocaleString()} points</small></button>}</For>
            <p class="explorer-note">{recording()!.alignment}. Sensor clock offsets are not corrected.</p>
            <Show when={recording()!.unaligned > 0}><p class="explorer-note">{recording()!.unaligned} readings omitted because GPS was unavailable.</p></Show>
          </Show>
          <Show when={tab() !== 'data'}>
            <label class="explorer-label">Data stream<select aria-label="Data stream" value={topic()}
              onChange={event => selectTopic(event.currentTarget.value)}>
              <For each={recording()!.datasets}>{data => <option value={data.topic}>{data.topic}</option>}</For></select></label>
            <label class="explorer-label">{tab() === 'layers' ? 'Color by' : 'Filter field'}<select aria-label="Reading field" value={fieldName()}
              onChange={event => selectField(event.currentTarget.value)}>
              <For each={dataset()?.fields ?? []}>{item => <option value={item.name}>{label(item.name)}{item.unit ? ` (${item.unit})` : ''}</option>}</For></select></label>
          </Show>
          <Show when={tab() === 'layers'}>
            <div class="explorer-layer"><label><input type="checkbox" checked={pointsVisible()}
              onChange={event => setPointsVisible(event.currentTarget.checked)} />Reading points</label>
              <span>{visibleCount().toLocaleString()}</span></div>
            <label class="explorer-label">Point size <span>{radius()} px</span><input aria-label="Reading point size" type="range" min="1" max="10" step="0.5"
              value={radius()} onInput={event => setRadius(Number(event.currentTarget.value))} /></label>
            <label class="explorer-label">Opacity <span>{Math.round(opacity() * 100)}%</span><input aria-label="Reading opacity" type="range" min="0.1" max="1" step="0.05"
              value={opacity()} onInput={event => setOpacity(Number(event.currentTarget.value))} /></label>
            <div class="explorer-layer"><label><input type="checkbox" checked={trackVisible()}
              onChange={event => setTrackVisible(event.currentTarget.checked)} />Vehicle track</label></div>
            <div class="explorer-color-scale" /><div class="explorer-scale-labels"><span>{field()?.min.toFixed(2)}</span><span>{field()?.unit}</span><span>{field()?.max.toFixed(2)}</span></div>
            <p class="explorer-note">Points show individual readings. Hover a point to inspect its values.</p>
          </Show>
          <Show when={tab() === 'filters'}>
            <h4>Value range {field()?.unit && `(${field()!.unit})`}</h4>
            <div class="explorer-range"><label>Minimum<input aria-label="Minimum reading value" type="number" step="any" value={minimum()}
              onInput={event => { const value = event.currentTarget.valueAsNumber; if (Number.isFinite(value)) setMinimum(value); }} /></label>
              <label>Maximum<input aria-label="Maximum reading value" type="number" step="any" value={maximum()}
              onInput={event => { const value = event.currentTarget.valueAsNumber; if (Number.isFinite(value)) setMaximum(value); }} /></label></div>
            <Show when={minimum() > maximum()}><p class="explorer-error">Minimum must be no greater than maximum.</p></Show>
            <button class="explorer-reset" onClick={() => selectField(fieldName())}><RotateCcw size={14} />Reset range</button>
            <p class="explorer-note">{visibleCount().toLocaleString()} matching readings at the current playback time. Bounds are inclusive.</p>
          </Show>
        </Show>
      </div>
      <Show when={recording()}>
        <div class="explorer-playback"><div class="explorer-playback-heading"><h4>Playback</h4><span>{timeLabel(elapsed())} / {timeLabel(recording()!.duration)}</span></div>
          <input aria-label="Recording playback time" type="range" min="0" max={recording()!.duration} step="0.1" value={elapsed()}
            onInput={event => { setPlaying(false); setElapsed(Number(event.currentTarget.value)); }} />
          <div class="explorer-playback-buttons"><button aria-label={playing() ? 'Pause replay' : 'Play replay'} onClick={() => {
            if (!playing() && elapsed() >= recording()!.duration) setElapsed(0);
            setPlaying(!playing());
          }}>{playing() ? <Pause size={16} /> : <Play size={16} />}{playing() ? 'Pause' : 'Play'}</button>
            <select aria-label="Playback speed" value={speed()} onChange={event => setSpeed(Number(event.currentTarget.value))}>
              <For each={[0.5, 1, 2, 4, 10]}>{value => <option value={value}>{value}×</option>}</For></select>
            <button class="icon-button" title="Fit recording on map" aria-label="Fit recording on map" onClick={() => props.onFit(recording()!)}><LocateFixed size={16} /></button></div>
        </div>
      </Show>
    </div>
  </Show>;
}
