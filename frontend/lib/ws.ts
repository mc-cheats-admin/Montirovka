import type { JobProgressEvent } from "./types";

/**
 * Builds the HTTP API base URL from public runtime environment.
 *
 * The frontend may work in several deployment modes:
 * - Next.js dev server with a relative API proxy;
 * - Docker setup with explicit NEXT_PUBLIC_API_BASE_URL;
 * - reverse-proxied production deployment.
 *
 * This helper mirrors the behavior already used in frontend/lib/api.ts
 * to keep URL construction consistent across REST and WebSocket layers.
 */
function getApiBaseUrl(): string {
  const rawValue = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();

  if (!rawValue) {
    return "";
  }

  return rawValue.endsWith("/") ? rawValue.slice(0, -1) : rawValue;
}

/**
 * Type guard for plain object-like values.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Converts an HTTP(S) origin into a WS(S) origin.
 */
function toWebSocketBaseUrl(httpBaseUrl: string): string {
  if (!httpBaseUrl) {
    if (typeof window === "undefined") {
      return "ws://localhost:8000";
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}`;
  }

  if (httpBaseUrl.startsWith("https://")) {
    return `wss://${httpBaseUrl.slice("https://".length)}`;
  }

  if (httpBaseUrl.startsWith("http://")) {
    return `ws://${httpBaseUrl.slice("http://".length)}`;
  }

  if (httpBaseUrl.startsWith("wss://") || httpBaseUrl.startsWith("ws://")) {
    return httpBaseUrl;
  }

  return httpBaseUrl;
}

/**
 * Builds an absolute WebSocket URL for job progress events.
 *
 * Backend contract:
 * GET /api/v1/jobs/{job_id}/events
 * WebSocket endpoint
 */
function buildJobEventsSocketUrl(jobId: string): string {
  const baseUrl = getApiBaseUrl();

  /**
   * Relative API mode:
   * When NEXT_PUBLIC_API_BASE_URL is empty, the browser origin is used.
   * In many local setups the frontend proxies /api to the backend, which
   * makes the same-origin socket URL the safest default.
   */
  if (!baseUrl) {
    if (typeof window === "undefined") {
      return `ws://localhost:3000/api/v1/jobs/${encodeURIComponent(jobId)}/events`;
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}/api/v1/jobs/${encodeURIComponent(jobId)}/events`;
  }

  return `${toWebSocketBaseUrl(baseUrl)}/api/v1/jobs/${encodeURIComponent(jobId)}/events`;
}

/**
 * Tries to normalize a raw incoming WebSocket payload into a JobProgressEvent.
 *
 * The backend sends two categories of messages:
 * - job progress events with fields like status/current_stage/progress_percent;
 * - heartbeat messages with shape { type: "heartbeat", job_id: "..." }.
 *
 * Heartbeats are intentionally ignored by returning null.
 */
function normalizeJobProgressEvent(rawPayload: unknown): JobProgressEvent | null {
  if (!isRecord(rawPayload)) {
    return null;
  }

  if (rawPayload.type === "heartbeat") {
    return null;
  }

  const jobId =
    typeof rawPayload.job_id === "string" ? rawPayload.job_id : null;
  const status =
    typeof rawPayload.status === "string" ? rawPayload.status : null;
  const currentStage =
    typeof rawPayload.current_stage === "string" ? rawPayload.current_stage : null;
  const progressPercentValue = rawPayload.progress_percent;
  const message =
    typeof rawPayload.message === "string" ? rawPayload.message : "";
  const timestamp =
    typeof rawPayload.timestamp === "string"
      ? rawPayload.timestamp
      : new Date().toISOString();

  if (!jobId || !status || !currentStage) {
    return null;
  }

  const progressPercent =
    typeof progressPercentValue === "number" && Number.isFinite(progressPercentValue)
      ? Math.max(0, Math.min(100, Math.round(progressPercentValue)))
      : 0;

  return {
    job_id: jobId,
    status,
    current_stage: currentStage,
    progress_percent: progressPercent,
    message,
    timestamp,
  };
}

/**
 * Safely parses WebSocket message data.
 *
 * Browser WebSocket events may deliver:
 * - string JSON;
 * - Blob;
 * - ArrayBuffer;
 * depending on server and browser behavior.
 *
 * This helper only supports string payloads directly and silently ignores
 * unsupported binary frames because the backend protocol is JSON-only.
 */
function parseSocketMessageData(data: unknown): unknown {
  if (typeof data === "string") {
    try {
      return JSON.parse(data) as unknown;
    } catch {
      return null;
    }
  }

  return null;
}

/**
 * Creates a WebSocket connection for real-time job events.
 *
 * Public behavior:
 * - returns the raw WebSocket instance so callers can manage lifecycle;
 * - forwards valid progress events to onMessage;
 * - forwards socket error events to onError;
 * - silently ignores heartbeat and malformed frames.
 *
 * The function intentionally does not implement reconnection by itself.
 * Reconnection policy belongs to UI components such as JobStatusClient,
 * which already own the page state and retry timing.
 */
export function createJobEventsSocket(
  jobId: string,
  onMessage: (event: JobProgressEvent) => void,
  onError: (error: Event) => void,
): WebSocket {
  const socketUrl = buildJobEventsSocketUrl(jobId);
  const socket = new WebSocket(socketUrl);

  socket.addEventListener("message", (event: MessageEvent) => {
    const parsedPayload = parseSocketMessageData(event.data);
    const normalizedEvent = normalizeJobProgressEvent(parsedPayload);

    if (!normalizedEvent) {
      return;
    }

    onMessage(normalizedEvent);
  });

  socket.addEventListener("error", (event: Event) => {
    onError(event);
  });

  return socket;
}

export { buildJobEventsSocketUrl };