import { createSignal, For, lazy, onCleanup, onMount, Show, Suspense } from 'solid-js';
import { invoke } from '@tauri-apps/api/core';
import { open, save } from '@tauri-apps/plugin-dialog';
import ConfirmSlide from './ConfirmSlide';
import type { Connection, Telemetry } from './types';

const MissionMap = lazy(() => import('./MissionMap'));

export default function App() {
  const [connection, setConnection] = createSignal<Connection>();
  const [state, setState] = createSignal<Telemetry>();
  const [bridgeError, setBridgeError] = createSignal('Starting Python backend…');
  const [commandError, setCommandError] = createSignal('');
  const [manualEnabled, setManualEnabled] = createSignal(false);
  const [networkBusy, setNetworkBusy] = createSignal(false);
  const [networkResult, setNetworkResult] = createSignal('');
  const [requested, setRequested] = createSignal({ throttle: 0, yaw: 0, source: 'KEYBOARD' });
  const keys = new Set<string>();
  let stopped = false;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let manualTimer: ReturnType<typeof setInterval> | undefined;
  let sending = false;
  let dirty = false;
  let manualChangedAt = 0;

  const command = async (action: string, data: Record<string, unknown> = {}) => {
    try {
      await invoke('backend_request', { action, data });
      setCommandError('');
    } catch (error) { setCommandError(String(error)); }
  };

  async function poll() {
    const pollStartedAt = performance.now();
    try {
      if (!connection()) setConnection(await invoke<Connection>('backend_info'));
      const next = await invoke<Telemetry>('backend_request');
      if (stopped) return;
      setState(next);
      setBridgeError('');
      if (pollStartedAt >= manualChangedAt && (next.mode !== 'MANUAL' || !next.manual.active)) {
        setManualEnabled(false); keys.clear();
        setRequested({ throttle: 0, yaw: 0, source: 'KEYBOARD' });
      }
    } catch (error) {
      if (!stopped) {
        setBridgeError(String(error));
        keys.clear(); setManualEnabled(false);
        setRequested({ throttle: 0, yaw: 0, source: 'KEYBOARD' });
      }
    }
    if (!stopped) pollTimer = setTimeout(poll, connection() ? 100 : 500);
  }

  // Keep at most one axes request in flight. Release/blur updates replace
  // queued axes instead of allowing stale throttle requests to arrive later.
  async function flushManual() {
    dirty = true;
    if (sending || !connection()) return;
    sending = true;
    try {
      while (dirty && !stopped) {
        dirty = false;
        await invoke('backend_request', { action: 'manual_axes', data: requested() });
      }
    } catch (error) { setCommandError(String(error)); }
    finally { sending = false; }
  }

  function axes(throttle: number, yaw: number) {
    setRequested({ throttle, yaw, source: 'KEYBOARD' });
    void flushManual();
  }

  function release() {
    manualChangedAt = performance.now();
    keys.clear(); setManualEnabled(false);
    axes(0, 0);
    void command('manual_active', { active: false });
  }

  function keyboard(event: KeyboardEvent, pressed: boolean) {
    if (event.code === 'Space' && pressed && !event.repeat) {
      event.preventDefault(); release(); void command('stop'); return;
    }
    const key = event.key.toUpperCase();
    if (!manualEnabled() || !['ARROWUP', 'ARROWDOWN', 'ARROWLEFT', 'ARROWRIGHT', 'W', 'A', 'S', 'D'].includes(key)) return;
    event.preventDefault();
    if (pressed) keys.add(key); else keys.delete(key);
    const held = (...values: string[]) => values.some(value => keys.has(value));
    axes(held('ARROWUP', 'W') ? 1000 : held('ARROWDOWN', 'S') ? -1000 : 0,
      held('ARROWRIGHT', 'D') ? 1000 : held('ARROWLEFT', 'A') ? -1000 : 0);
  }

  const keyDown = (event: KeyboardEvent) => keyboard(event, true);
  const keyUp = (event: KeyboardEvent) => keyboard(event, false);
  const visibility = () => { if (document.hidden) release(); };
  onMount(() => {
    void poll();
    manualTimer = setInterval(() => {
      if (manualEnabled() && (requested().throttle || requested().yaw)) void flushManual();
    }, 100);
    window.addEventListener('keydown', keyDown);
    window.addEventListener('keyup', keyUp);
    window.addEventListener('blur', release);
    document.addEventListener('visibilitychange', visibility);
  });
  onCleanup(() => {
    release(); stopped = true;
    clearTimeout(pollTimer); clearInterval(manualTimer);
    window.removeEventListener('keydown', keyDown);
    window.removeEventListener('keyup', keyUp);
    window.removeEventListener('blur', release);
    document.removeEventListener('visibilitychange', visibility);
  });

  async function missionFile(action: 'load' | 'save' | 'export_lake') {
    // Native dialogs temporarily take focus: release controls first.
    release();
    try {
      const path = action === 'load' ? await open({ multiple: false, filters: [
        { name: 'Mission files', extensions: ['geojson', 'json', 'csv'] },
      ] }) : await save({ defaultPath: action === 'save' ? 'mission_saved.geojson' : 'fiu_lake_safety_areas.geojson',
        filters: [{ name: 'GeoJSON', extensions: ['geojson'] }] });
      if (typeof path === 'string') await command(action, { path });
    } catch (error) { setCommandError(String(error)); }
  }

  async function enableManual() {
    try {
      await invoke('backend_request', { action: 'manual', data: {} });
      manualChangedAt = performance.now();
      setManualEnabled(true); setCommandError('');
    } catch (error) { setCommandError(String(error)); }
  }

  async function configureNetwork() {
    release(); setNetworkBusy(true); setNetworkResult('');
    try { setNetworkResult(await invoke<string>('network_setup')); }
    catch (error) { setNetworkResult(String(error)); }
    finally { setNetworkBusy(false); }
  }

  return <div class="app">
    <header>
      <div class="brand">GROUND STATION</div>
      <div class="header-status">
        <span class="badge" classList={{ online: state()?.connected }}><i />{state()?.connected ? 'ONLINE' : 'CONNECTING'}</span>
        <span class="badge" classList={{ armed: state()?.armed }}>{state()?.armed ? 'ARMED' : 'DISARMED'}</span>
        <span class="battery">BATTERY {state() && state()!.battery_pct >= 0 ? `${state()!.battery_pct}%` : '--'}</span>
        <strong>{state()?.mode ?? 'STARTING'}</strong>
        <Show when={state()?.simulating}><span class="badge simulation">SIMULATION</span></Show>
      </div>
    </header>
    <main>
      <aside>
        <div class="eyebrow workspace-label">Workspace</div>
        <section class="telemetry panel">
          <h2>Live telemetry</h2>
          <div class="telemetry-grid"><div><b>{state()?.speed.toFixed(1) ?? '—'}</b><small>m/s</small></div>
            <div><b>{state()?.heading.toFixed(0) ?? '—'}°</b><small>heading</small></div>
            <div><b>{state()?.satellites ?? '—'}</b><small>satellites</small></div></div>
          <p class="coordinates">{state() && (state()!.lat || state()!.lon) ? `${state()!.lat.toFixed(6)}, ${state()!.lon.toFixed(6)}` : 'Waiting for GPS'}</p>
          <small>{state()?.waypoints.length ?? 0} waypoints · {state()?.battery_v.toFixed(1) ?? '—'} V · GPS fix {state()?.fix ?? '—'}</small>
        </section>
        <section class="route-controls">
          <h2>Route</h2>
          <div class="button-grid">
            <button class="span-two" disabled={!!bridgeError() || state()?.uploading || state()?.starting}
              onClick={() => void missionFile('load')}>Load GeoJSON</button>
            <div class="span-two"><ConfirmSlide label={state()?.starting ? 'Starting mission…' : 'Hold & slide to start'}
              disabled={!state()?.waypoints.length || state()?.starting || state()?.uploading || !!bridgeError()}
              onConfirm={() => { release(); void command('start'); }} /></div>
            <button disabled={!!bridgeError()} onClick={() => void missionFile('save')}>Save GeoJSON</button>
            <button disabled={!state()?.close_available} onClick={() => void command('close_loop')}>Close loop</button>
            <button class="span-two" disabled={!state()?.clear_available} onClick={() => void command('clear')}>Clear route</button>
          </div>
          <div class="route-state">{state()?.starting || state()?.uploading ? 'Preparing mission…' :
            state()?.waypoints.length ? 'Start Mission uploads the route automatically' : 'Load a file or add waypoints on the map'}</div>
        </section>
        <section>
          <h2>Vehicle</h2>
          <ConfirmSlide label={state()?.armed ? 'Hold & slide to disarm' : 'Hold & slide to arm'} reverse={state()?.armed}
            disabled={!!bridgeError()} onConfirm={() => void command(state()?.armed ? 'disarm' : 'arm')} />
          <button class="stop vehicle-stop" disabled={!!bridgeError()}
            onClick={() => { release(); void command('stop'); }}>Stop vehicle</button>
        </section>
        <section class="manual-controls panel">
          <div class="manual-heading">
            <div class="manual-title"><h2>Manual input</h2>
              <span class="input-source" title={state()?.manual.source ?? 'KEYBOARD'}>{state()?.manual.source ?? 'KEYBOARD'}</span>
            </div>
            <label><input type="checkbox" aria-label="Enable manual input" checked={manualEnabled()} disabled={!!bridgeError()}
              onChange={event => event.currentTarget.checked ? void enableManual() : release()} />Enable</label>
          </div>
          <p class="route-state">{state()?.gamepad?.connected
            ? `${state()!.gamepad.name} connected`
            : state()?.gamepad?.error
              ? `Controller unavailable: ${state()!.gamepad.error}`
              : 'No controller connected · reconnecting automatically'}</p>
          <div class="manual-axes"><For each={['throttle', 'yaw'] as const}>{axis => <label class="axis">
            <span>{axis === 'throttle' ? 'Throttle' : 'Yaw / steering'}<b>{state()?.manual[axis] ?? 0}</b></span>
            <input class="axis-indicator" type="range" aria-label={`${axis} input indicator`}
              aria-readonly="true" min="-1000" max="1000" step="1" disabled
              value={state()?.manual[axis] ?? 0} />
          </label>}</For></div>
          <small class="manual-hint">WASD / arrows · Space to stop</small>
        </section>
        <section class="activity"><h2>Activity</h2><pre>{state()?.logs.join('\n') || 'Waiting for backend…'}</pre></section>
        <div class="utility-buttons"><button disabled={networkBusy() || !!bridgeError()} onClick={() => void configureNetwork()}>
          {networkBusy() ? 'Configuring…' : 'Configure network'}</button>
          </div>
      </aside>
      <Show when={connection()} fallback={<div class="starting"><span class="brand-mark">H</span><p>{bridgeError()}</p></div>}>
        <Suspense fallback={<div class="starting">Loading map…</div>}>
          <MissionMap connection={connection()!} state={state()} command={command}
            exportEnabled={!bridgeError()} onExportLake={() => void missionFile('export_lake')} />
        </Suspense>
      </Show>
    </main>
    <Show when={networkResult()}><div class="network-result glass"><button onClick={() => setNetworkResult('')}>Close</button><pre>{networkResult()}</pre></div></Show>
    <footer classList={{ error: !!bridgeError() || !!commandError() || state()?.status_error }}>
      <span>{bridgeError() || commandError() || state()?.status || 'Click map to add · Drag markers to edit · Right-click markers to remove'}</span>
      <kbd>SPACE TO STOP</kbd>
    </footer>
  </div>;
}
