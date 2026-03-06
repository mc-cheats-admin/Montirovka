/**
 * Shared frontend TypeScript types for the AutoEdit application.
 *
 * This file is the central contract layer for:
 * - REST API responses consumed by frontend/lib/api.ts;
 * - WebSocket event payloads consumed by frontend/lib/ws.ts;
 * - React client components such as EditorClient and JobStatusClient;
 * - page-level data composition for result and preview screens.
 *
 * Design goals:
 * - stay closely aligned with the backend specification;
 * - remain flexible enough for partially available payloads during processing;
 * - avoid `any` and keep all public structures explicitly typed;
 * - provide reusable JSON-like utility types for analysis/result objects.
 */

/**
 * Primitive JSON value type used as a building block for flexible
 * backend-driven metadata blobs such as analysis and preset settings.
 */
export type JsonPrimitive = string | number | boolean | null;

/**
 * Recursive JSON value type.
 *
 * This is intentionally useful for:
 * - analysis payloads returned by the backend;
 * - preset default settings;
 * - result metadata objects;
 * - generic nested runtime settings.
 */
export type JsonValue = JsonPrimitive | JsonObject | JsonArray;

/**
 * JSON object shape with string keys.
 */
export interface JsonObject {
  [key: string]: JsonValue;
}

/**
 * JSON array shape.
 */
export interface JsonArray extends Array<JsonValue> {}

/**
 * Allowed built-in preset names.
 *
 * These names must stay synchronized with:
 * - backend validation;
 * - preset JSON files;
 * - API request payloads;
 * - UI selection controls.
 */
export type PresetName = "gaming" | "tutorial" | "cinematic";

/**
 * Full lifecycle status of a processing job.
 *
 * The values are taken directly from the backend domain model and are used
 * both for REST polling and WebSocket progress updates.
 */
export type JobStatus =
  | "uploaded"
  | "queued"
  | "analyzing"
  | "cutting"
  | "enhancing"
  | "interpolating"
  | "processing_audio"
  | "generating_subtitles"
  | "rendering"
  | "generating_preview"
  | "completed"
  | "failed"
  | "cancelled";

/**
 * Supported target FPS values exposed in the advanced settings form.
 */
export type TargetFps = 24 | 30 | 60 | 120;

/**
 * Supported output aspect ratio choices exposed in the UI.
 */
export type OutputAspectRatio = "16:9" | "21:9" | "9:16";

/**
 * Supported output codec values for final rendering.
 */
export type OutputCodec = "h264" | "h265";

/**
 * Upload response returned by POST /api/v1/uploads.
 *
 * The payload contains metadata about the uploaded media file that is later
 * used when creating a processing job.
 */
export interface UploadResponse {
  file_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
}

/**
 * Frontend representation of user-adjustable job settings.
 *
 * All fields are optional in the backend specification, because the backend
 * can merge them with preset defaults. On the frontend we still keep the same
 * contract so components can send only the values they actually want to set.
 */
export interface JobSettingsFormValue {
  target_fps?: TargetFps;
  zoom_scale?: number;
  cut_aggressiveness?: number;
  noise_reduction_enabled?: boolean;
  subtitles_enabled?: boolean;
  output_aspect_ratio?: OutputAspectRatio;
  codec?: OutputCodec;
}

/**
 * Structured error payload attached to a job response when processing fails.
 */
export interface JobErrorInfo {
  error_code: string;
  message: string;
}

/**
 * Flexible result payload returned by the backend for a completed job.
 *
 * The project specification allows result information to evolve over time.
 * To keep the frontend type-safe but not brittle, the interface supports a
 * few common optional fields plus generic metadata keys.
 */
export interface JobResultInfo extends JsonObject {
  output_file_id?: string;
  preview_file_id?: string;
  subtitle_file_id?: string;
  before_file_id?: string;
  after_file_id?: string;
  thumbnail_file_id?: string;
  download_url?: string;
  subtitle_url?: string;
  output_filename?: string;
  subtitle_filename?: string;
}

/**
 * Detailed job response returned by:
 * - POST /api/v1/jobs
 * - GET /api/v1/jobs/{job_id}
 *
 * Some fields such as analysis, result and error may be null depending on the
 * current lifecycle stage of the job.
 */
export interface JobResponse {
  job_id: string;
  status: JobStatus;
  current_stage: JobStatus | string;
  progress_percent: number;
  preset_name: PresetName | string;
  analysis: JsonObject | null;
  result: JobResultInfo | null;
  error: JobErrorInfo | null;

  /**
   * Optional timestamps and metadata that may be present in richer backend
   * responses. They are marked optional so the current UI can safely consume
   * either minimal or extended payloads.
   */
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  original_filename?: string | null;
}

/**
 * One built-in preset returned by GET /api/v1/presets.
 *
 * `default_settings` is intentionally modeled as a generic JSON object because
 * preset files contain nested structures that vary significantly per preset.
 */
export interface PresetItem {
  name: PresetName;
  display_name: string;
  default_settings: JsonObject;
}

/**
 * Real-time WebSocket progress event for a job.
 *
 * Although the backend conceptual type includes a datetime, the frontend
 * receives it as an ISO 8601 string and therefore stores it as `string`.
 */
export interface JobProgressEvent {
  job_id: string;
  status: JobStatus;
  current_stage: JobStatus | string;
  progress_percent: number;
  message: string;
  timestamp: string;
}