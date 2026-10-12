import { createEffect, createSignal, onCleanup, onMount, Show } from 'solid-js';
import { LocateFixed, Minus, Plus, X } from 'lucide-solid';
import * as maplibregl from 'maplibre-gl';
import type { GeoJSONSource, Map as LibreMap, Marker } from 'maplibre-gl';
import workerAsset from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import type { FeatureCollection } from 'geojson';
import type { Connection, SendCommand, Telemetry } from './types';
import LakePadding from './LakePadding';

const empty: FeatureCollection = { type: 'FeatureCollection', features: [] };

export default function MissionMap(props: {
  connection: Connection; state: Telemetry | undefined; command: SendCommand;
  exportEnabled: boolean; onExportLake: () => void;
}) {
  let container!: HTMLDivElement;
  let map: LibreMap | undefined;
  let markers: Marker[] = [];
  let vehicle: Marker | undefined;
  let dragging = -1;
  let follow = true;
  let located = false;
  let routeVersion = '';
  let lakeVersion = '';
  let workerBlob: string | undefined;
  let disposed = false;
  const [loaded, setLoaded] = createSignal(false);
  const [zoom, setZoom] = createSignal(2);
  const [mapError, setMapError] = createSignal('');
  const [following, setFollowing] = createSignal(true);

  function center() {
    const state = props.state;
    if (!map || !state) return;
    const position = (state.lat || state.lon) ? [state.lon, state.lat] :
      state.desktop_location ? [state.desktop_location.lon, state.desktop_location.lat] : null;
    if (position) {
      const current = map.getCenter();
      if (!located || Math.abs(current.lng - position[0]) > 1e-8 || Math.abs(current.lat - position[1]) > 1e-8) {
        map.jumpTo({ center: position as [number, number], ...(!located ? { zoom: 19 } : {}) });
      }
      located = true;
    }
  }

  onMount(async () => {
    // MapLibre 6 resolves its default worker beside the module. Bundling
    // changes that path, and WebKit can't load a worker from tauri://.
    // Ship the worker as an asset and instantiate it from a local blob.
    try {
      const response = await fetch(workerAsset);
      if (!response.ok) throw new Error('Map worker asset is unavailable');
      const code = await response.text();
      if (disposed) return;
      workerBlob = URL.createObjectURL(new Blob([code], { type: 'text/javascript' }));
      maplibregl.setWorkerUrl(workerBlob);
    } catch (error) { setMapError(String(error)); return; }
    const root = `http://127.0.0.1:${props.connection.port}`;
    const token = encodeURIComponent(props.connection.token);
    map = new maplibregl.Map({
      container, center: [0, 0], zoom: 2, minZoom: 1, maxZoom: 22,
      attributionControl: false, dragRotate: false, pitchWithRotate: false,
      style: {
        version: 8,
        sources: {
          esri: { type: 'raster', tiles: [`${root}/tiles/esri/{z}/{x}/{y}.png?token=${token}`],
            tileSize: 256, maxzoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics, and the GIS User Community' },
          fiu: { type: 'raster', tiles: [`${root}/tiles/fiu/{z}/{x}/{y}.png?token=${token}`],
            tileSize: 256, maxzoom: 19, bounds: [-80.4, 25.74, -80.35, 25.78],
            attribution: 'FIU · Vivid Standard imagery' },
          lake: { type: 'geojson', data: empty },
          route: { type: 'geojson', data: empty },
        },
        layers: [
          { id: 'background', type: 'background', paint: { 'background-color': '#171b20' } },
          { id: 'esri', type: 'raster', source: 'esri', paint: { 'raster-brightness-max': 0.85 } },
          { id: 'fiu', type: 'raster', source: 'fiu', paint: { 'raster-brightness-max': 0.85 } },
          { id: 'safe-fill', type: 'fill', source: 'lake', filter: ['==', ['get', 'role'], 'safe_area'],
            paint: { 'fill-color': '#1ebe7d', 'fill-opacity': 0.15 } },
          { id: 'safe-line', type: 'line', source: 'lake', filter: ['==', ['get', 'role'], 'safe_area'],
            paint: { 'line-color': '#3ddca0', 'line-width': 2 } },
          { id: 'shore', type: 'line', source: 'lake', filter: ['==', ['get', 'role'], 'shoreline'],
            paint: { 'line-color': '#59e1e5', 'line-width': 2, 'line-dasharray': [3, 2] } },
          { id: 'fountain-fill', type: 'fill', source: 'lake', filter: ['==', ['get', 'role'], 'fountain_safety_area'],
            paint: { 'fill-color': '#ff7d48', 'fill-opacity': 0.16 } },
          { id: 'fountain-line', type: 'line', source: 'lake', filter: ['==', ['get', 'role'], 'fountain_safety_area'],
            paint: { 'line-color': '#ff9b69', 'line-width': 1.5 } },
          { id: 'fountain-center', type: 'circle', source: 'lake', filter: ['==', ['get', 'role'], 'fountain_center'],
            paint: { 'circle-color': '#ffb07f', 'circle-radius': 4 } },
          { id: 'route', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#ffd84a', 'line-width': 3 } },
        ],
      },
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    map.on('load', () => { setLoaded(true); center(); });
    map.on('error', () => setMapError('Some imagery tiles could not load. Local FIU imagery remains available.'));
    map.on('zoom', () => setZoom(map!.getZoom()));
    map.on('dragstart', () => { follow = false; setFollowing(false); });
    // MapLibre's default drag-pan uses the left button. Preserve the old
    // ground station's middle/right-button panning on empty map space too.
    const canvas = map.getCanvas();
    let alternatePan: { x: number; y: number; pointer: number } | undefined;
    canvas.addEventListener('pointerdown', event => {
      if (event.button !== 1 && event.button !== 2) return;
      event.preventDefault();
      follow = false; setFollowing(false);
      alternatePan = { x: event.clientX, y: event.clientY, pointer: event.pointerId };
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', event => {
      if (!alternatePan || alternatePan.pointer !== event.pointerId) return;
      map!.panBy([alternatePan.x - event.clientX, alternatePan.y - event.clientY], { duration: 0 });
      alternatePan.x = event.clientX; alternatePan.y = event.clientY;
    });
    const endPan = () => { alternatePan = undefined; };
    canvas.addEventListener('pointerup', endPan);
    canvas.addEventListener('pointercancel', endPan);
    canvas.addEventListener('contextmenu', event => event.preventDefault());
    map.on('click', event => {
      if (dragging >= 0 || props.state?.uploading || props.state?.starting) return;
      void props.command('add_waypoint', { lat: event.lngLat.lat, lon: event.lngLat.lng });
    });
  });

  createEffect(() => {
    const state = props.state;
    if (!loaded() || !map || !state) return;
    const lake = JSON.stringify(state.lake);
    if (lake !== lakeVersion) {
      (map.getSource('lake') as GeoJSONSource).setData(state.lake);
      lakeVersion = lake;
    }
    const route = JSON.stringify(state.waypoints);
    if (route !== routeVersion && dragging < 0) {
      (map.getSource('route') as GeoJSONSource).setData({ type: 'FeatureCollection', features:
        state.waypoints.length > 1 ? [{ type: 'Feature', properties: {}, geometry: {
          type: 'LineString', coordinates: state.waypoints.map(wp => [wp.lon, wp.lat]),
        } }] : [] });
      while (markers.length > state.waypoints.length) markers.pop()!.remove();
      state.waypoints.forEach((wp, index) => {
        if (!markers[index]) {
          const element = document.createElement('button');
          element.className = 'waypoint';
          element.textContent = String(index + 1);
          element.title = `Waypoint ${index + 1}: drag to move, right-click to delete`;
          element.addEventListener('click', event => event.stopPropagation());
          element.addEventListener('contextmenu', event => {
            event.preventDefault(); event.stopPropagation();
            void props.command('remove_waypoint', { index });
          });
          markers[index] = new maplibregl.Marker({ element, draggable: true })
            .setLngLat([wp.lon, wp.lat]).addTo(map!);
          markers[index].on('dragstart', () => { dragging = index; });
          markers[index].on('dragend', () => {
            const position = markers[index].getLngLat();
            void props.command('move_waypoint', { index, lat: position.lat, lon: position.lng });
            // Don't let the release click create an extra waypoint.
            setTimeout(() => { dragging = -1; routeVersion = ''; }, 0);
          });
        }
        markers[index].setLngLat([wp.lon, wp.lat]);
      });
      routeVersion = route;
    }
    markers.forEach((marker, index) => {
      marker.setDraggable(!state.uploading && !state.starting);
      marker.getElement().classList.toggle('active', state.current_wp === index + 1);
    });
    if (state.lat || state.lon) {
      if (!vehicle) {
        const element = document.createElement('div');
        element.className = 'vehicle-marker'; element.title = 'Heron vehicle';
        vehicle = new maplibregl.Marker({ element, rotationAlignment: 'map' })
          .setLngLat([state.lon, state.lat]).addTo(map);
      }
      vehicle.setLngLat([state.lon, state.lat]).setRotation(state.heading);
    }
    if (follow) center();
  });

  onCleanup(() => {
    disposed = true;
    markers.forEach(marker => marker.remove()); vehicle?.remove(); map?.remove();
    if (workerBlob) URL.revokeObjectURL(workerBlob);
  });

  return <div class="map-workspace">
    <div ref={container} class="map" aria-label="Mission planning map" />
    <div class="map-hint glass">Click to add waypoints · Drag to pan · Pinch to zoom</div>
    <LakePadding value={props.state?.padding ?? 2} enabled={!!props.state?.padding_enabled} command={props.command}
      exportEnabled={props.exportEnabled} onExport={props.onExportLake} />
    <Show when={props.state?.tile_error || mapError()}>
      <div class="map-warning glass">{props.state?.tile_error || mapError()}<button aria-label="Dismiss map warning" onClick={() => setMapError('')}><X size={16} aria-hidden="true" /></button></div>
    </Show>
    <div class="map-navigation glass">
      <button aria-label="Zoom in" title="Zoom in" onClick={() => map?.zoomIn()}><Plus size={18} aria-hidden="true" /></button>
      <button aria-label="Zoom out" title="Zoom out" onClick={() => map?.zoomOut()}><Minus size={18} aria-hidden="true" /></button>
      <button aria-label="Recenter on vehicle" title="Follow vehicle" aria-pressed={following()} classList={{ selected: following() }}
        onClick={() => { follow = true; setFollowing(true); center(); }}><LocateFixed size={18} aria-hidden="true" /></button>
      <button aria-label="Center on FIU lake" title="Center on FIU lake" onClick={() => {
        follow = false; located = true; setFollowing(false);
        map?.flyTo({ center: [-80.37365, 25.7581], zoom: 19 });
      }}>FIU</button>
    </div>
    <div class="map-source glass">Esri + FIU <span>Z {zoom().toFixed(1)}</span></div>
  </div>;
}
