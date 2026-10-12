import type { FeatureCollection } from 'geojson';

export interface Waypoint { id: number; lat: number; lon: number; alt: number }
export interface Telemetry {
  connected: boolean; armed: boolean; mode: string;
  lat: number; lon: number; alt: number; heading: number; speed: number;
  satellites: number; fix: number; battery_v: number; battery_pct: number;
  current_wp: number; waypoints: Waypoint[];
  uploaded: boolean; uploading: boolean; started: boolean; starting: boolean;
  simulating: boolean; clear_available: boolean; close_available: boolean;
  padding_enabled: boolean; padding: number; lake: FeatureCollection;
  status: string; status_error: boolean; logs: string[]; tile_error: string;
  manual: { throttle: number; yaw: number; source: string; active: boolean };
  gamepad: { connected: boolean; name: string | null; error: string | null };
  desktop_location: { lat: number; lon: number } | null;
}
export interface Connection { port: number; token: string }
export type SendCommand = (action: string, data?: Record<string, unknown>) => Promise<void>;
