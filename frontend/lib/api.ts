import type {
  JobResponse,
  JobSettingsFormValue,
  PresetItem,
  PresetName,
  UploadResponse,
} from "./types";

/**
 * Public preview payload returned by the backend preview endpoint.
 */
export interface JobPreviewResponse {
  before_url: string;
  after_url: string;
  thumbnail_url: string;
}

/**
 * Payload used by the createJob helper.
 *
 * The shape mirrors the backend API contract exactly and is kept separate
 * from the raw function arguments for better reusability in UI code.
 */
export interface CreateJobPayload {
  file_id: string;
  preset_name: PresetName;
  settings: JobSettingsFormValue;
}

/**
 * Small structured API error used across the frontend data layer.
 *
 * Keeping a dedicated error class makes it easier for UI components to:
 * - show human-friendly messages;
 * - inspect response status codes if needed later;
 * - preserve backend error_code values when available.
 */
export class ApiError extends Error {
  public readonly status: number;
  public readonly errorCode: string | null;
  public readonly details: unknown;

  constructor(message: string, status = 0, errorCode: string | null = null, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = errorCode;
    this.details = details;
  }
}

const API_PREFIX = "/api/v1";

/**
 * Returns the configured API base URL.
 *
 * The frontend can run in different environments:
 * - locally via Next.js dev server;
 * - inside Docker with a proxied API;
 * - against a remote backend.
 *
 * If NEXT_PUBLIC_API_BASE_URL is missing, the client falls back to a relative
 * path, which works well when the frontend is reverse-proxying the backend.
 */
function getApiBaseUrl(): string {
  const rawValue = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();

  if (!rawValue) {
    return "";
  }

  return rawValue.endsWith("/") ? rawValue.slice(0, -1) : rawValue;
}

/**
 * Builds a fully qualified API URL from a relative endpoint path.
 */
function buildApiUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${getApiBaseUrl()}${normalizedPath}`;
}

/**
 * Best-effort type guard for plain object-like values.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Attempts to parse the response body based on the declared content type.
 *
 * For the endpoints in this project, JSON is the primary format. Still, this
 * helper remains defensive and can also safely handle text or empty responses.
 */
async function parseResponseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";

  if (contentType.includes("application/json")) {
    return response.json();
  }

  if (contentType.includes("text/")) {
    return response.text();
  }

  if (response.status === 204) {
    return null;
  }

  try {
    return await response.text();
  } catch {
    return null;
  }
}

/**
 * Converts backend and transport failures into a user-friendly ApiError.
 *
 * The backend specification mentions a structured payload:
 * {
 *   "error_code": "...",
 *   "message": "..."
 * }
 *
 * This helper preserves that data when present, but also provides a graceful
 * fallback message for unexpected responses.
 */
async function buildApiError(response: Response): Promise<ApiError> {
  const body = await parseResponseBody(response);

  if (isRecord(body)) {
    const errorCode =
      typeof body.error_code === "string" ? body.error_code : null;
    const message =
      typeof body.message === "string"
        ? body.message
        : typeof body.detail === "string"
          ? body.detail
          : `Ошибка запроса (${response.status})`;

    return new ApiError(message, response.status, errorCode, body);
  }

  if (typeof body === "string" && body.trim().length > 0) {
    return new ApiError(body, response.status, null, body);
  }

  return new ApiError(`Ошибка запроса (${response.status})`, response.status, null, body);
}

/**
 * Executes an HTTP request against the backend and returns parsed JSON data.
 *
 * The function is intentionally generic and strict:
 * - throws ApiError for non-2xx responses;
 * - throws ApiError for network failures;
 * - keeps credentials handling explicit and predictable.
 */
async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(buildApiUrl(path), {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiError(
      error instanceof Error
        ? `Сетевой запрос не выполнен: ${error.message}`
        : "Сетевой запрос не выполнен.",
      0,
      "NETWORK_ERROR",
      error,
    );
  }

  if (!response.ok) {
    throw await buildApiError(response);
  }

  const data = await parseResponseBody(response);
  return data as T;
}

/**
 * Returns a safe MIME type for a selected file.
 *
 * Browsers may leave File.type empty for some files. The backend still performs
 * authoritative validation, but sending a sensible fallback improves behavior.
 */
function resolveUploadMimeType(file: File): string {
  if (file.type && file.type.trim().length > 0) {
    return file.type;
  }

  const lowerName = file.name.toLowerCase();

  if (lowerName.endsWith(".mp4")) {
    return "video/mp4";
  }

  if (lowerName.endsWith(".mov")) {
    return "video/quicktime";
  }

  if (lowerName.endsWith(".avi")) {
    return "video/x-msvideo";
  }

  if (lowerName.endsWith(".mkv")) {
    return "video/x-matroska";
  }

  return "application/octet-stream";
}

/**
 * Uploads a raw video file to the backend.
 *
 * Backend contract:
 * POST /api/v1/uploads
 * multipart/form-data with field "file"
 */
export async function uploadVideo(file: File): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("file", file, file.name);

  let response: Response;

  try {
    response = await fetch(buildApiUrl(`${API_PREFIX}/uploads`), {
      method: "POST",
      body: formData,
      headers: {
        Accept: "application/json",
      },
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiError(
      error instanceof Error
        ? `Не удалось загрузить файл: ${error.message}`
        : "Не удалось загрузить файл.",
      0,
      "NETWORK_ERROR",
      error,
    );
  }

  if (!response.ok) {
    throw await buildApiError(response);
  }

  const payload = (await parseResponseBody(response)) as UploadResponse;

  /**
   * Some backends rely on file.type; others inspect the file directly.
   * This branch is not used to mutate the sent request, but keeps future
   * integration points easy to extend without changing public behavior.
   */
  void resolveUploadMimeType(file);

  return payload;
}

/**
 * Loads the list of built-in processing presets.
 *
 * Expected backend payload:
 * {
 *   "items": [...]
 * }
 */
export async function fetchPresets(): Promise<PresetItem[]> {
  const payload = await requestJson<{ items?: PresetItem[] }>(`${API_PREFIX}/presets`);

  if (isRecord(payload) && Array.isArray(payload.items)) {
    return payload.items;
  }

  throw new ApiError(
    "Сервер вернул некорректный список пресетов.",
    500,
    "INVALID_PRESET_RESPONSE",
    payload,
  );
}

/**
 * Creates a processing job for a previously uploaded media file.
 */
export async function createJob(payload: CreateJobPayload): Promise<JobResponse> {
  return requestJson<JobResponse>(`${API_PREFIX}/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * Fetches the current state of a job by its identifier.
 */
export async function fetchJob(jobId: string): Promise<JobResponse> {
  return requestJson<JobResponse>(`${API_PREFIX}/jobs/${encodeURIComponent(jobId)}`);
}

/**
 * Fetches preview asset URLs for a completed job.
 */
export async function fetchPreview(jobId: string): Promise<JobPreviewResponse> {
  return requestJson<JobPreviewResponse>(
    `${API_PREFIX}/jobs/${encodeURIComponent(jobId)}/preview`,
  );
}

/**
 * Builds a direct download URL for the final rendered video.
 *
 * The URL is returned as a plain string because it is typically used directly
 * in anchors or button href values without an extra fetch roundtrip.
 */
export function buildDownloadUrl(jobId: string): string {
  return buildApiUrl(`${API_PREFIX}/jobs/${encodeURIComponent(jobId)}/download`);
}

/**
 * Builds a direct media URL for subtitle or sidecar file download.
 *
 * According to the backend API, subtitle files are exposed through the generic
 * media endpoint rather than a dedicated subtitle route.
 */
export function buildSubtitleUrl(fileId: string): string {
  return buildApiUrl(`${API_PREFIX}/results/media/${encodeURIComponent(fileId)}`);
}

export type { JobSettingsFormValue, JobResponse, PresetItem, PresetName, UploadResponse };