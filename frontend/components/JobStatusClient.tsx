"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { fetchJob } from "../lib/api";
import type { JobProgressEvent, JobResponse, JobStatus } from "../lib/types";
import { createJobEventsSocket } from "../lib/ws";

type JobStatusClientProps = {
  jobId: string;
};

type ConnectionState = "idle" | "connecting" | "connected" | "reconnecting" | "disconnected";

type StatusTone = "neutral" | "info" | "success" | "error" | "warning";

/**
 * Human-readable labels for backend pipeline stages.
 *
 * The labels are aligned with the statuses described in the project
 * specification and match the terminology already used in EditorClient.
 */
const STAGE_LABELS: Record<JobStatus, string> = {
  uploaded: "Файл загружен",
  queued: "В очереди",
  analyzing: "Анализ видео",
  cutting: "Умная нарезка",
  enhancing: "Улучшение качества",
  interpolating: "Интерполяция FPS",
  processing_audio: "Обработка аудио",
  generating_subtitles: "Генерация субтитров",
  rendering: "Рендер видео",
  generating_preview: "Подготовка превью",
  completed: "Готово",
  failed: "Ошибка",
  cancelled: "Отменено",
};

/**
 * Converts a job status to a UI tone used by the badge component.
 */
function getStatusTone(status: JobStatus): StatusTone {
  if (status === "completed") {
    return "success";
  }

  if (status === "failed") {
    return "error";
  }

  if (status === "cancelled") {
    return "warning";
  }

  if (status === "queued" || status === "uploaded") {
    return "neutral";
  }

  return "info";
}

/**
 * Formats ISO date strings from the API for compact Russian UI output.
 */
function formatDateTime(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(parsed);
}

/**
 * Returns a safe, clamped progress value from unknown backend state.
 */
function normalizeProgress(progress: number | null | undefined, status: JobStatus): number {
  if (status === "completed") {
    return 100;
  }

  if (typeof progress !== "number" || Number.isNaN(progress)) {
    return 0;
  }

  return Math.min(100, Math.max(0, Math.round(progress)));
}

/**
 * Best-effort extraction of the current user-facing message from
 * either WebSocket events or available job data.
 */
function buildFallbackMessage(job: JobResponse | null): string | null {
  if (!job) {
    return null;
  }

  if (job.error?.message) {
    return job.error.message;
  }

  if (job.status === "completed") {
    return "Обработка завершена. Можно перейти к результату.";
  }

  if (job.status === "failed") {
    return "Во время обработки произошла ошибка.";
  }

  if (job.status === "cancelled") {
    return "Задача была отменена.";
  }

  return "Задача выполняется на сервере. Прогресс обновляется автоматически.";
}

/**
 * Main client component for the job status page.
 *
 * Responsibilities:
 * - load current job snapshot on mount;
 * - subscribe to backend WebSocket events for real-time updates;
 * - reconnect automatically if the socket drops unexpectedly;
 * - display progress, current stage, technical metadata and failure details;
 * - provide navigation to the result page once processing is complete.
 */
export default function JobStatusClient({ jobId }: JobStatusClientProps) {
  const [job, setJob] = useState<JobResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [lastMessage, setLastMessage] = useState<string | null>(null);
  const [lastEventTimestamp, setLastEventTimestamp] = useState<string | null>(null);

  const [connectionState, setConnectionState] = useState<ConnectionState>("idle");
  const [socketAttempt, setSocketAttempt] = useState<number>(0);

  const reconnectTimerRef = useRef<number | null>(null);
  const hasTerminalState = job
    ? job.status === "completed" || job.status === "failed" || job.status === "cancelled"
    : false;

  /**
   * Clears any scheduled reconnect timer to avoid duplicated socket attempts.
   */
  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  /**
   * Loads the current job state from the REST API.
   *
   * This initial snapshot is important because:
   * - the page must render useful information before the first WS event;
   * - users may open the page in the middle of processing;
   * - the socket may temporarily fail, while REST still provides status.
   */
  const loadJob = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);

    try {
      const data = await fetchJob(jobId);
      setJob(data);
      setLastMessage((current) => current ?? buildFallbackMessage(data));
    } catch (error) {
      setLoadError(
        error instanceof Error
          ? error.message
          : "Не удалось получить информацию о задаче.",
      );
    } finally {
      setIsLoading(false);
    }
  }, [jobId]);

  /**
   * Initial load on mount and when jobId changes.
   */
  useEffect(() => {
    void loadJob();
  }, [loadJob]);

  /**
   * Keeps message aligned with terminal or newly loaded states when no
   * WebSocket message has been received yet.
   */
  useEffect(() => {
    if (!lastMessage) {
      setLastMessage(buildFallbackMessage(job));
    }
  }, [job, lastMessage]);

  /**
   * WebSocket subscription with lightweight auto-reconnect.
   *
   * The component intentionally reconnects only while the job is non-terminal.
   * Once the backend reports completed/failed/cancelled, the connection can be
   * safely closed and no additional retries are needed.
   */
  useEffect(() => {
    if (!jobId || hasTerminalState) {
      setConnectionState(hasTerminalState ? "disconnected" : "idle");
      clearReconnectTimer();
      return;
    }

    setConnectionState(socketAttempt === 0 ? "connecting" : "reconnecting");

    const socket = createJobEventsSocket(
      jobId,
      (event: JobProgressEvent) => {
        setConnectionState("connected");
        setLastMessage(event.message ?? null);
        setLastEventTimestamp(event.timestamp ?? null);

        setJob((current) => {
          const existing = current;

          if (!existing) {
            return {
              job_id: event.job_id,
              status: event.status,
              current_stage: event.current_stage,
              progress_percent: event.progress_percent,
              preset_name: "gaming",
              analysis: null,
              result: null,
              error: null,
            };
          }

          return {
            ...existing,
            status: event.status,
            current_stage: event.current_stage,
            progress_percent: event.progress_percent,
            error:
              event.status === "failed"
                ? existing.error ?? {
                    error_code: "INTERNAL_SERVER_ERROR",
                    message: event.message || "Во время обработки произошла ошибка.",
                  }
                : existing.error,
          };
        });

        if (
          event.status === "completed" ||
          event.status === "failed" ||
          event.status === "cancelled"
        ) {
          clearReconnectTimer();
          void loadJob();
          socket.close();
        }
      },
      () => {
        setConnectionState("disconnected");
      },
    );

    socket.onopen = () => {
      setConnectionState("connected");
    };

    socket.onerror = () => {
      setConnectionState("disconnected");
    };

    socket.onclose = () => {
      if (hasTerminalState) {
        setConnectionState("disconnected");
        return;
      }

      setConnectionState("disconnected");
      clearReconnectTimer();

      reconnectTimerRef.current = window.setTimeout(() => {
        setSocketAttempt((current) => current + 1);
      }, 2000);
    };

    return () => {
      clearReconnectTimer();
      socket.close();
    };
  }, [clearReconnectTimer, hasTerminalState, jobId, loadJob, socketAttempt]);

  const status = job?.status ?? "queued";
  const currentStage = job?.current_stage ?? "queued";
  const progressPercent = normalizeProgress(job?.progress_percent, status);
  const stageLabel = STAGE_LABELS[currentStage] ?? currentStage;
  const statusTone = getStatusTone(status);

  const analysisSummary = useMemo(() => {
    if (!job?.analysis) {
      return [];
    }

    const entries: Array<{ label: string; value: string }> = [];

    if (typeof job.analysis.width === "number" && typeof job.analysis.height === "number") {
      entries.push({
        label: "Разрешение",
        value: `${job.analysis.width}×${job.analysis.height}`,
      });
    }

    if (typeof job.analysis.fps === "number") {
      entries.push({
        label: "FPS исходника",
        value: `${job.analysis.fps}`,
      });
    }

    if (typeof job.analysis.duration_seconds === "number") {
      entries.push({
        label: "Длительность",
        value: `${job.analysis.duration_seconds.toFixed(2)} сек`,
      });
    }

    if (typeof job.analysis.audio_peak_db === "number") {
      entries.push({
        label: "Пик аудио",
        value: `${job.analysis.audio_peak_db.toFixed(1)} dB`,
      });
    }

    if (typeof job.analysis.estimated_noise_floor_db === "number") {
      entries.push({
        label: "Шумовой пол",
        value: `${job.analysis.estimated_noise_floor_db.toFixed(1)} dB`,
      });
    }

    if (Array.isArray(job.analysis.silence_segments)) {
      entries.push({
        label: "Сегменты тишины",
        value: `${job.analysis.silence_segments.length}`,
      });
    }

    if (Array.isArray(job.analysis.scene_changes)) {
      entries.push({
        label: "Смена сцен",
        value: `${job.analysis.scene_changes.length}`,
      });
    }

    return entries;
  }, [job?.analysis]);

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
      <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 shadow-[0_10px_30px_rgba(0,0,0,0.25)] sm:p-6">
        <div className="flex flex-col gap-4 border-b border-white/10 pb-5 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">Статус задачи</p>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-[#F3F4F6] sm:text-3xl">
              Обработка видео
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-7 text-[#A5B4CC]">
              Следите за этапами пайплайна AutoEdit в реальном времени. Страница
              автоматически обновляет прогресс через WebSocket и показывает итоговый
              переход к результату после завершения.
            </p>
          </div>

          <StatusBadge label={STAGE_LABELS[status] ?? status} tone={statusTone} />
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <InfoCard label="Job ID" value={jobId} mono />
          <InfoCard
            label="Пресет"
            value={job?.preset_name ? humanizePresetName(job.preset_name) : "—"}
          />
          <InfoCard label="Этап" value={stageLabel} />
          <InfoCard label="Соединение" value={humanizeConnectionState(connectionState)} />
        </div>

        <div className="mt-6 rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="text-sm font-medium text-[#F3F4F6]">Прогресс обработки</div>
              <div className="mt-1 text-sm text-[#A5B4CC]">{stageLabel}</div>
            </div>

            <div className="text-right">
              <div className="text-3xl font-semibold text-[#F3F4F6]">{progressPercent}%</div>
              <div className="text-xs text-[#A5B4CC]">
                {lastEventTimestamp
                  ? `Последнее событие: ${formatDateTime(lastEventTimestamp) ?? "—"}`
                  : "Ожидание событий"}
              </div>
            </div>
          </div>

          <div className="mt-4 h-3 overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full bg-gradient-to-r from-[#7C5CFF] to-[#00C2FF] transition-all duration-500 ease-out"
              style={{ width: `${progressPercent}%` }}
              aria-hidden="true"
            />
          </div>

          <p className="mt-4 text-sm leading-7 text-[#A5B4CC]">
            {lastMessage ?? buildFallbackMessage(job) ?? "Подготовка данных задачи."}
          </p>
        </div>

        {isLoading ? (
          <LoadingPanel />
        ) : loadError ? (
          <ErrorPanel
            title="Не удалось загрузить статус задачи"
            message={loadError}
            onRetry={() => {
              void loadJob();
            }}
          />
        ) : null}

        {job?.error ? (
          <div className="mt-6 rounded-[18px] border border-red-500/25 bg-red-500/10 p-4 sm:p-5">
            <div className="text-sm font-semibold text-red-300">Ошибка обработки</div>
            <div className="mt-2 text-sm text-red-100/90">
              <span className="font-medium">Код:</span> {job.error.error_code}
            </div>
            <p className="mt-2 text-sm leading-7 text-red-100/90">{job.error.message}</p>
          </div>
        ) : null}

        {analysisSummary.length > 0 ? (
          <div className="mt-6">
            <h2 className="text-lg font-semibold text-[#F3F4F6]">Результаты анализа</h2>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {analysisSummary.map((item) => (
                <article
                  key={item.label}
                  className="rounded-2xl border border-white/10 bg-[#182235]/70 p-4"
                >
                  <div className="text-xs uppercase tracking-[0.16em] text-[#A5B4CC]">
                    {item.label}
                  </div>
                  <div className="mt-2 text-base font-semibold text-[#F3F4F6]">
                    {item.value}
                  </div>
                </article>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      <aside className="flex flex-col gap-5">
        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 shadow-[0_10px_30px_rgba(0,0,0,0.25)] sm:p-6">
          <h2 className="text-lg font-semibold text-[#F3F4F6]">Действия</h2>
          <div className="mt-4 flex flex-col gap-3">
            <Link
              href="/"
              className="inline-flex items-center justify-center rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-[#F3F4F6] transition hover:bg-white/10"
            >
              Обработать новое видео
            </Link>

            <Link
              href={`/results/${jobId}`}
              aria-disabled={status !== "completed"}
              className={[
                "inline-flex items-center justify-center rounded-xl px-4 py-3 text-sm font-medium transition",
                status === "completed"
                  ? "bg-gradient-to-r from-[#7C5CFF] to-[#00C2FF] text-white hover:opacity-95"
                  : "cursor-not-allowed border border-white/10 bg-white/5 text-[#A5B4CC]",
              ].join(" ")}
            >
              Перейти к результату
            </Link>
          </div>

          <div className="mt-4 rounded-2xl border border-white/10 bg-[#182235]/70 p-4">
            <div className="text-sm font-medium text-[#F3F4F6]">Что происходит сейчас</div>
            <p className="mt-2 text-sm leading-7 text-[#A5B4CC]">
              {lastMessage ?? "Ожидание обновлений от сервера."}
            </p>
          </div>
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 shadow-[0_10px_30px_rgba(0,0,0,0.25)] sm:p-6">
          <h2 className="text-lg font-semibold text-[#F3F4F6]">Этапы пайплайна</h2>
          <ol className="mt-4 space-y-3">
            {(
              [
                "queued",
                "analyzing",
                "cutting",
                "enhancing",
                "interpolating",
                "processing_audio",
                "generating_subtitles",
                "rendering",
                "generating_preview",
                "completed",
              ] satisfies JobStatus[]
            ).map((stageKey) => (
              <li
                key={stageKey}
                className={[
                  "rounded-2xl border px-4 py-3 text-sm transition",
                  stageKey === currentStage
                    ? "border-[#7C5CFF]/50 bg-[#7C5CFF]/10 text-[#F3F4F6]"
                    : "border-white/10 bg-[#182235]/60 text-[#A5B4CC]",
                ].join(" ")}
              >
                {STAGE_LABELS[stageKey]}
              </li>
            ))}
          </ol>
        </section>
      </aside>
    </div>
  );
}

type SectionInfoCardProps = {
  label: string;
  value: string;
  mono?: boolean;
};

/**
 * Compact key-value card used for job metadata.
 */
function InfoCard({ label, value, mono = false }: SectionInfoCardProps) {
  return (
    <article className="rounded-2xl border border-white/10 bg-[#182235]/70 p-4">
      <div className="text-xs uppercase tracking-[0.16em] text-[#A5B4CC]">{label}</div>
      <div
        className={[
          "mt-2 text-sm font-medium text-[#F3F4F6]",
          mono ? "break-all font-mono text-xs sm:text-sm" : "",
        ].join(" ")}
      >
        {value}
      </div>
    </article>
  );
}

type StatusBadgeProps = {
  label: string;
  tone: StatusTone;
};

/**
 * Small reusable badge for visual status emphasis.
 */
function StatusBadge({ label, tone }: StatusBadgeProps) {
  const toneClasses: Record<StatusTone, string> = {
    neutral: "border-white/10 bg-white/5 text-[#F3F4F6]",
    info: "border-cyan-400/25 bg-cyan-400/10 text-cyan-200",
    success: "border-green-500/25 bg-green-500/10 text-green-300",
    error: "border-red-500/25 bg-red-500/10 text-red-300",
    warning: "border-amber-500/25 bg-amber-500/10 text-amber-300",
  };

  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium",
        toneClasses[tone],
      ].join(" ")}
    >
      {label}
    </span>
  );
}

type ErrorPanelProps = {
  title: string;
  message: string;
  onRetry: () => void;
};

/**
 * Error state block with a retry action.
 */
function ErrorPanel({ title, message, onRetry }: ErrorPanelProps) {
  return (
    <div className="mt-6 rounded-[18px] border border-red-500/25 bg-red-500/10 p-4 sm:p-5">
      <div className="text-base font-semibold text-red-300">{title}</div>
      <p className="mt-2 text-sm leading-7 text-red-100/90">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 inline-flex items-center justify-center rounded-xl border border-red-400/30 bg-red-500/15 px-4 py-2 text-sm font-medium text-red-100 transition hover:bg-red-500/20"
      >
        Повторить запрос
      </button>
    </div>
  );
}

/**
 * Lightweight skeleton/loading block shown while initial job data is loading.
 */
function LoadingPanel() {
  return (
    <div className="mt-6 animate-pulse rounded-[18px] border border-white/10 bg-[#182235]/50 p-4 sm:p-5">
      <div className="h-4 w-40 rounded bg-white/10" />
      <div className="mt-4 h-3 w-full rounded-full bg-white/10" />
      <div className="mt-4 h-4 w-3/4 rounded bg-white/10" />
      <div className="mt-2 h-4 w-2/3 rounded bg-white/10" />
    </div>
  );
}

/**
 * Converts preset names to Russian-friendly labels.
 */
function humanizePresetName(value: string): string {
  if (value === "gaming") {
    return "Gaming / Highlight";
  }

  if (value === "tutorial") {
    return "Tutorial / Обучение";
  }

  if (value === "cinematic") {
    return "Cinematic / Контент";
  }

  return value;
}

/**
 * Maps raw socket state to readable UI text.
 */
function humanizeConnectionState(state: ConnectionState): string {
  if (state === "connecting") {
    return "Подключение…";
  }

  if (state === "connected") {
    return "Подключено";
  }

  if (state === "reconnecting") {
    return "Переподключение…";
  }

  if (state === "disconnected") {
    return "Отключено";
  }

  return "Ожидание";
}