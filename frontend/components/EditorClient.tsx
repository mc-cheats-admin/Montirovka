"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { DragEvent, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { createJob, fetchPresets, uploadVideo } from "../lib/api";
import type {
  JobResponse,
  JobSettingsFormValue,
  PresetItem,
  PresetName,
  UploadResponse,
} from "../lib/types";

const ACCEPTED_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv"] as const;
const MAX_SIZE_BYTES = 2 * 1024 * 1024 * 1024;

const PRESET_DESCRIPTIONS: Record<PresetName, string> = {
  gaming:
    "Для динамичных игровых хайлайтов: приоритет на high FPS, активные jump-cut и акценты на ярких моментах.",
  tutorial:
    "Для обучающих видео: агрессивное удаление пауз, улучшение речи и подготовка субтитров.",
  cinematic:
    "Для контентных роликов и влогов: мягкий монтаж, цветокоррекция и более кинематографичная подача.",
};

const STAGE_LABELS: Record<string, string> = {
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
 * Returns default form settings for the selected preset.
 *
 * The values intentionally mirror the product requirements:
 * - gaming defaults to higher FPS and no subtitles;
 * - tutorial defaults to 60 FPS and subtitles enabled;
 * - cinematic defaults to 24 FPS and a more conservative processing profile.
 */
function getDefaultSettingsForPreset(presetName: PresetName): JobSettingsFormValue {
  if (presetName === "gaming") {
    return {
      target_fps: 120,
      zoom_scale: 1.3,
      cut_aggressiveness: 0.7,
      noise_reduction_enabled: true,
      subtitles_enabled: false,
      output_aspect_ratio: "16:9",
      codec: "h264",
    };
  }

  if (presetName === "tutorial") {
    return {
      target_fps: 60,
      zoom_scale: 1.1,
      cut_aggressiveness: 0.85,
      noise_reduction_enabled: true,
      subtitles_enabled: true,
      output_aspect_ratio: "16:9",
      codec: "h264",
    };
  }

  return {
    target_fps: 24,
    zoom_scale: 1.0,
    cut_aggressiveness: 0.35,
    noise_reduction_enabled: false,
    subtitles_enabled: false,
    output_aspect_ratio: "21:9",
    codec: "h265",
  };
}

/**
 * Human-friendly file size formatting for UI display.
 */
function formatBytes(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = sizeBytes;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  const precision = value >= 100 || unitIndex === 0 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(precision)} ${units[unitIndex]}`;
}

/**
 * Validates a file selected on the client before upload.
 *
 * Client-side validation is advisory and UX-oriented. The backend remains the
 * authoritative source of truth for size, MIME type and extension checks.
 */
function validateSelectedFile(file: File): string | null {
  const lowerName = file.name.toLowerCase();
  const hasSupportedExtension = ACCEPTED_EXTENSIONS.some((extension) =>
    lowerName.endsWith(extension),
  );

  if (!hasSupportedExtension) {
    return "Неподдерживаемый формат. Разрешены только .mp4, .mov, .avi и .mkv.";
  }

  if (file.size <= 0) {
    return "Пустой файл нельзя отправить на обработку.";
  }

  if (file.size > MAX_SIZE_BYTES) {
    return "Файл слишком большой. Максимальный размер — 2 GB.";
  }

  return null;
}

type SubmitState = "idle" | "uploading" | "creating_job" | "redirecting";

type FormFieldBooleanKey = "noise_reduction_enabled" | "subtitles_enabled";
type FormFieldNumberKey = "zoom_scale" | "cut_aggressiveness";
type FormFieldSelectKey = "target_fps" | "output_aspect_ratio" | "codec";

/**
 * Main interactive editor component used on the homepage.
 *
 * Responsibilities:
 * - load preset definitions from the backend;
 * - accept and validate a local video file;
 * - expose advanced runtime settings in a compact but clear form;
 * - upload the file and create a processing job;
 * - redirect the user to the job tracking page after successful creation.
 *
 * This component intentionally remains self-contained because the current
 * generation step only includes this file. It can later be split into smaller
 * UI components without changing the public behavior.
 */
export default function EditorClient() {
  const router = useRouter();

  const [presets, setPresets] = useState<PresetItem[]>([]);
  const [presetsLoading, setPresetsLoading] = useState<boolean>(true);
  const [presetsError, setPresetsError] = useState<string | null>(null);

  const [selectedPreset, setSelectedPreset] = useState<PresetName>("gaming");
  const [settings, setSettings] = useState<JobSettingsFormValue>(
    getDefaultSettingsForPreset("gaming"),
  );

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileValidationError, setFileValidationError] = useState<string | null>(null);

  const [uploadedFile, setUploadedFile] = useState<UploadResponse | null>(null);
  const [createdJob, setCreatedJob] = useState<JobResponse | null>(null);

  const [submitState, setSubmitState] = useState<SubmitState>("idle");
  const [submitError, setSubmitError] = useState<string | null>(null);

  const isBusy = submitState !== "idle";
  const canSubmit = Boolean(selectedFile) && !fileValidationError && !isBusy;

  /**
   * Loads available presets from the backend once on mount.
   *
   * If the request fails, the UI still remains usable thanks to local fallback
   * defaults for the three known presets, but a visible warning is shown.
   */
  useEffect(() => {
    let isMounted = true;

    const loadPresets = async () => {
      setPresetsLoading(true);
      setPresetsError(null);

      try {
        const items = await fetchPresets();
        if (!isMounted) {
          return;
        }

        setPresets(items);
      } catch (error) {
        if (!isMounted) {
          return;
        }

        setPresetsError(
          error instanceof Error
            ? error.message
            : "Не удалось загрузить список пресетов.",
        );

        setPresets([
          {
            name: "gaming",
            display_name: "Gaming / Highlight",
            default_settings: getDefaultSettingsForPreset("gaming"),
          },
          {
            name: "tutorial",
            display_name: "Tutorial / Обучение",
            default_settings: getDefaultSettingsForPreset("tutorial"),
          },
          {
            name: "cinematic",
            display_name: "Cinematic / Контент",
            default_settings: getDefaultSettingsForPreset("cinematic"),
          },
        ]);
      } finally {
        if (isMounted) {
          setPresetsLoading(false);
        }
      }
    };

    void loadPresets();

    return () => {
      isMounted = false;
    };
  }, []);

  /**
   * Keeps local settings aligned with the currently selected preset.
   *
   * The reset is intentional because presets represent opinionated pipelines.
   * Users can still fine-tune the values immediately after switching.
   */
  useEffect(() => {
    setSettings(getDefaultSettingsForPreset(selectedPreset));
  }, [selectedPreset]);

  const selectedPresetMeta = useMemo(
    () => presets.find((preset) => preset.name === selectedPreset) ?? null,
    [presets, selectedPreset],
  );

  const selectedStageLabel = createdJob
    ? STAGE_LABELS[createdJob.current_stage] ?? createdJob.current_stage
    : "Ещё не запущено";

  /**
   * Applies client-side validation and stores the selected file.
   */
  const handleFileSelected = useCallback((file: File) => {
    const validationError = validateSelectedFile(file);
    setSelectedFile(file);
    setUploadedFile(null);
    setCreatedJob(null);
    setSubmitError(null);
    setFileValidationError(validationError);
  }, []);

  /**
   * Handles files dropped onto the custom dropzone.
   */
  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();

      const file = event.dataTransfer.files?.[0];
      if (!file) {
        return;
      }

      handleFileSelected(file);
    },
    [handleFileSelected],
  );

  /**
   * Prevents the browser from opening the file when dragging over the zone.
   */
  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
  }, []);

  /**
   * Updates a boolean field in the advanced settings form.
   */
  const updateBooleanField = useCallback(
    (field: FormFieldBooleanKey, value: boolean) => {
      setSettings((current) => ({
        ...current,
        [field]: value,
      }));
    },
    [],
  );

  /**
   * Updates a numeric field in the advanced settings form.
   */
  const updateNumberField = useCallback(
    (field: FormFieldNumberKey, value: number) => {
      setSettings((current) => ({
        ...current,
        [field]: value,
      }));
    },
    [],
  );

  /**
   * Updates a select/radio field in the advanced settings form.
   */
  const updateSelectField = useCallback(
    (
      field: FormFieldSelectKey,
      value: JobSettingsFormValue[FormFieldSelectKey],
    ) => {
      setSettings((current) => ({
        ...current,
        [field]: value,
      }));
    },
    [],
  );

  /**
   * Resets the current form to a clean state while preserving the active preset.
   */
  const resetEditor = useCallback(() => {
    setSelectedFile(null);
    setFileValidationError(null);
    setUploadedFile(null);
    setCreatedJob(null);
    setSubmitError(null);
    setSubmitState("idle");
    setSettings(getDefaultSettingsForPreset(selectedPreset));
  }, [selectedPreset]);

  /**
   * Uploads the selected video file and creates a new processing job.
   *
   * The flow is deliberately sequential:
   * 1. upload media and receive file metadata / file_id;
   * 2. create a job bound to that uploaded file;
   * 3. redirect user to the job status page.
   */
  const handleSubmit = useCallback(async () => {
    if (!selectedFile) {
      setSubmitError("Сначала выберите видеофайл.");
      return;
    }

    if (fileValidationError) {
      setSubmitError(fileValidationError);
      return;
    }

    setSubmitError(null);
    setCreatedJob(null);

    try {
      setSubmitState("uploading");
      const uploadResult = await uploadVideo(selectedFile);
      setUploadedFile(uploadResult);

      setSubmitState("creating_job");
      const jobResult = await createJob({
        file_id: String(uploadResult.file_id),
        preset_name: selectedPreset,
        settings,
      });

      setCreatedJob(jobResult);

      setSubmitState("redirecting");
      router.push(`/jobs/${jobResult.job_id}`);
    } catch (error) {
      setSubmitState("idle");
      setSubmitError(
        error instanceof Error
          ? error.message
          : "Не удалось запустить обработку. Попробуйте ещё раз.",
      );
    }
  }, [fileValidationError, router, selectedFile, selectedPreset, settings]);

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
      <div className="flex flex-col gap-5">
        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 sm:p-6">
          <SectionHeading
            eyebrow="Шаг 1"
            title="Загрузите исходное видео"
            description="Поддерживаются .mp4, .mov, .avi и .mkv. Максимальный размер файла — 2 GB."
          />

          <div
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            className="mt-5 rounded-[18px] border border-dashed border-white/15 bg-[#182235] p-5 transition hover:border-[#7C5CFF]/60 hover:bg-[#1C2940] sm:p-6"
          >
            <label
              htmlFor="video-upload-input"
              className="block cursor-pointer"
            >
              <div className="flex flex-col items-center justify-center text-center">
                <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-[#7C5CFF] to-[#00C2FF] text-2xl text-white shadow-[0_10px_30px_rgba(124,92,255,0.25)]">
                  ⬆
                </div>

                <h3 className="mt-4 text-lg font-semibold text-[#F3F4F6]">
                  Перетащите видео сюда или выберите файл
                </h3>
                <p className="mt-2 max-w-2xl text-sm leading-6 text-[#A5B4CC]">
                  После загрузки файл будет отправлен на backend, а затем
                  поставлен в очередь на обработку с выбранным пресетом.
                </p>

                <div className="mt-5 inline-flex rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm font-medium text-[#F3F4F6]">
                  Выбрать файл
                </div>
              </div>

              <input
                id="video-upload-input"
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(",")}
                className="sr-only"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    handleFileSelected(file);
                  }
                }}
              />
            </label>

            <div className="mt-5 grid gap-3 sm:grid-cols-3">
              <InfoChip label="Форматы" value={ACCEPTED_EXTENSIONS.join(" ")} />
              <InfoChip label="Размер" value="до 2 GB" />
              <InfoChip label="Режим" value="Self-hosted" />
            </div>

            {selectedFile ? (
              <div className="mt-5 rounded-2xl border border-white/10 bg-[#101726] p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
                      Выбранный файл
                    </p>
                    <p className="mt-1 truncate text-sm font-medium text-[#F3F4F6]">
                      {selectedFile.name}
                    </p>
                    <p className="mt-2 text-sm text-[#A5B4CC]">
                      {formatBytes(selectedFile.size)}
                      {selectedFile.type ? ` • ${selectedFile.type}` : ""}
                    </p>
                  </div>

                  <button
                    type="button"
                    onClick={resetEditor}
                    className="inline-flex items-center justify-center rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm font-medium text-[#F3F4F6] transition hover:bg-white/10"
                  >
                    Очистить
                  </button>
                </div>
              </div>
            ) : null}

            {fileValidationError ? (
              <StatusMessage kind="error" className="mt-4">
                {fileValidationError}
              </StatusMessage>
            ) : null}
          </div>
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 sm:p-6">
          <SectionHeading
            eyebrow="Шаг 2"
            title="Выберите пресет обработки"
            description="Каждый пресет задаёт базовый сценарий пайплайна. После выбора можно скорректировать параметры вручную."
          />

          {presetsLoading ? (
            <div className="mt-5 grid gap-4 md:grid-cols-3">
              {Array.from({ length: 3 }).map((_, index) => (
                <div
                  key={index}
                  className="h-40 animate-pulse rounded-[18px] border border-white/10 bg-[#182235]"
                />
              ))}
            </div>
          ) : (
            <div className="mt-5 grid gap-4 md:grid-cols-3">
              {presets.map((preset) => {
                const isSelected = preset.name === selectedPreset;

                return (
                  <button
                    key={preset.name}
                    type="button"
                    onClick={() => setSelectedPreset(preset.name as PresetName)}
                    className={[
                      "group rounded-[18px] border p-5 text-left transition",
                      isSelected
                        ? "border-[#7C5CFF] bg-[linear-gradient(180deg,rgba(124,92,255,0.16),rgba(0,194,255,0.08))] shadow-[0_10px_30px_rgba(124,92,255,0.15)]"
                        : "border-white/10 bg-[#182235] hover:border-white/20 hover:bg-[#1C2940]",
                    ].join(" ")}
                    aria-pressed={isSelected}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-base font-semibold text-[#F3F4F6]">
                          {preset.display_name}
                        </div>
                        <p className="mt-3 text-sm leading-6 text-[#A5B4CC]">
                          {PRESET_DESCRIPTIONS[preset.name as PresetName] ??
                            "Пользовательский сценарий автоматического монтажа."}
                        </p>
                      </div>

                      <span
                        className={[
                          "mt-0.5 inline-flex h-6 w-6 flex-none items-center justify-center rounded-full border text-xs font-bold",
                          isSelected
                            ? "border-[#7C5CFF]/60 bg-[#7C5CFF]/20 text-white"
                            : "border-white/10 bg-white/5 text-[#A5B4CC]",
                        ].join(" ")}
                        aria-hidden="true"
                      >
                        {isSelected ? "✓" : "•"}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}

          {presetsError ? (
            <StatusMessage kind="warning" className="mt-4">
              Не удалось получить пресеты с backend, используются локальные
              fallback-настройки. Детали: {presetsError}
            </StatusMessage>
          ) : null}
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 sm:p-6">
          <SectionHeading
            eyebrow="Шаг 3"
            title="Тонкая настройка"
            description="Эти параметры переопределяют runtime-конфигурацию выбранного пресета перед постановкой задачи в очередь."
          />

          <div className="mt-5 grid gap-5">
            <FieldBlock
              label="Целевой FPS"
              description="Выберите итоговую частоту кадров для обработки и рендера."
            >
              <div className="flex flex-wrap gap-2">
                {[24, 30, 60, 120].map((fpsValue) => {
                  const active = settings.target_fps === fpsValue;

                  return (
                    <button
                      key={fpsValue}
                      type="button"
                      onClick={() =>
                        updateSelectField("target_fps", fpsValue as 24 | 30 | 60 | 120)
                      }
                      className={[
                        "rounded-xl border px-4 py-2 text-sm font-medium transition",
                        active
                          ? "border-[#7C5CFF] bg-[#7C5CFF]/20 text-white"
                          : "border-white/10 bg-[#182235] text-[#A5B4CC] hover:bg-[#1C2940]",
                      ].join(" ")}
                      aria-pressed={active}
                    >
                      {fpsValue} FPS
                    </button>
                  );
                })}
              </div>
            </FieldBlock>

            <div className="grid gap-5 md:grid-cols-2">
              <FieldBlock
                label="Интенсивность zoom"
                description="Используется для сценариев с focus/zoom. Диапазон от 1.0 до 2.0."
              >
                <RangeControl
                  min={1}
                  max={2}
                  step={0.1}
                  value={settings.zoom_scale ?? 1}
                  displayValue={`${(settings.zoom_scale ?? 1).toFixed(1)}x`}
                  onChange={(value) => updateNumberField("zoom_scale", value)}
                />
              </FieldBlock>

              <FieldBlock
                label="Агрессивность нарезки"
                description="Чем выше значение, тем смелее пайплайн вырезает паузы и слабые сегменты."
              >
                <RangeControl
                  min={0}
                  max={1}
                  step={0.05}
                  value={settings.cut_aggressiveness ?? 0}
                  displayValue={(settings.cut_aggressiveness ?? 0).toFixed(2)}
                  onChange={(value) =>
                    updateNumberField("cut_aggressiveness", value)
                  }
                />
              </FieldBlock>
            </div>

            <div className="grid gap-5 md:grid-cols-2">
              <FieldBlock
                label="Подавление шума"
                description="Включает локальную аудиообработку и noise reduction, если это поддерживается пресетом."
              >
                <ToggleCard
                  checked={Boolean(settings.noise_reduction_enabled)}
                  onChange={(value) =>
                    updateBooleanField("noise_reduction_enabled", value)
                  }
                  enabledLabel="Включено"
                  disabledLabel="Выключено"
                />
              </FieldBlock>

              <FieldBlock
                label="Субтитры"
                description="Генерация sidecar .srt через локальную Whisper-модель."
              >
                <ToggleCard
                  checked={Boolean(settings.subtitles_enabled)}
                  onChange={(value) => updateBooleanField("subtitles_enabled", value)}
                  enabledLabel="Включены"
                  disabledLabel="Выключены"
                />
              </FieldBlock>
            </div>

            <div className="grid gap-5 md:grid-cols-2">
              <FieldBlock
                label="Соотношение сторон"
                description="Определяет целевой формат кадра на этапе финального рендера."
              >
                <SelectControl
                  value={settings.output_aspect_ratio ?? "16:9"}
                  onChange={(value) =>
                    updateSelectField(
                      "output_aspect_ratio",
                      value as "16:9" | "21:9" | "9:16",
                    )
                  }
                  options={[
                    { label: "16:9", value: "16:9" },
                    { label: "21:9", value: "21:9" },
                    { label: "9:16", value: "9:16" },
                  ]}
                />
              </FieldBlock>

              <FieldBlock
                label="Кодек"
                description="H.264 — лучший баланс совместимости, H.265 — более эффективное сжатие."
              >
                <SelectControl
                  value={settings.codec ?? "h264"}
                  onChange={(value) =>
                    updateSelectField("codec", value as "h264" | "h265")
                  }
                  options={[
                    { label: "H.264", value: "h264" },
                    { label: "H.265", value: "h265" },
                  ]}
                />
              </FieldBlock>
            </div>
          </div>
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5 sm:p-6">
          <SectionHeading
            eyebrow="Шаг 4"
            title="Запуск обработки"
            description="После старта видео будет загружено на backend, задание попадёт в очередь Celery, а затем откроется страница отслеживания статуса."
          />

          <div className="mt-5 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div className="space-y-2 text-sm text-[#A5B4CC]">
              <p>
                Активный пресет:{" "}
                <span className="font-medium text-[#F3F4F6]">
                  {selectedPresetMeta?.display_name ?? selectedPreset}
                </span>
              </p>
              <p>
                Файл:{" "}
                <span className="font-medium text-[#F3F4F6]">
                  {selectedFile ? selectedFile.name : "не выбран"}
                </span>
              </p>
            </div>

            <div className="flex flex-col gap-3 sm:flex-row">
              <button
                type="button"
                onClick={resetEditor}
                disabled={isBusy}
                className="inline-flex items-center justify-center rounded-xl border border-white/10 bg-white/5 px-5 py-3 text-sm font-medium text-[#F3F4F6] transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Сбросить
              </button>

              <button
                type="button"
                onClick={() => void handleSubmit()}
                disabled={!canSubmit}
                className="inline-flex items-center justify-center rounded-xl bg-gradient-to-r from-[#7C5CFF] to-[#00C2FF] px-5 py-3 text-sm font-semibold text-white shadow-[0_10px_30px_rgba(124,92,255,0.25)] transition hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitState === "uploading" && "Загрузка файла..."}
                {submitState === "creating_job" && "Создание задания..."}
                {submitState === "redirecting" && "Переход к статусу..."}
                {submitState === "idle" && "Начать обработку"}
              </button>
            </div>
          </div>

          {submitError ? (
            <StatusMessage kind="error" className="mt-4">
              {submitError}
            </StatusMessage>
          ) : null}
        </section>
      </div>

      <aside className="flex flex-col gap-5">
        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5">
          <SectionHeading
            eyebrow="Сводка"
            title="Текущая конфигурация"
            description="Быстрый обзор того, что будет отправлено в API."
          />

          <dl className="mt-5 space-y-4">
            <SummaryRow label="Пресет" value={selectedPreset} />
            <SummaryRow label="FPS" value={String(settings.target_fps ?? "—")} />
            <SummaryRow
              label="Zoom"
              value={`${(settings.zoom_scale ?? 1).toFixed(1)}x`}
            />
            <SummaryRow
              label="Нарезка"
              value={(settings.cut_aggressiveness ?? 0).toFixed(2)}
            />
            <SummaryRow
              label="Шумодав"
              value={settings.noise_reduction_enabled ? "Да" : "Нет"}
            />
            <SummaryRow
              label="Субтитры"
              value={settings.subtitles_enabled ? "Да" : "Нет"}
            />
            <SummaryRow
              label="Aspect ratio"
              value={settings.output_aspect_ratio ?? "16:9"}
            />
            <SummaryRow label="Codec" value={settings.codec ?? "h264"} />
          </dl>
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5">
          <SectionHeading
            eyebrow="Статус"
            title="Последнее действие"
            description="Компонент показывает локальный статус до перехода на страницу задачи."
          />

          <div className="mt-5 rounded-2xl border border-white/10 bg-[#182235] p-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
                  Этап
                </p>
                <p className="mt-1 text-sm font-medium text-[#F3F4F6]">
                  {selectedStageLabel}
                </p>
              </div>

              <span
                className={[
                  "inline-flex rounded-full px-3 py-1 text-xs font-medium",
                  createdJob?.status === "completed"
                    ? "bg-[#22C55E]/15 text-[#86EFAC]"
                    : createdJob?.status === "failed"
                      ? "bg-[#EF4444]/15 text-[#FCA5A5]"
                      : "bg-[#7C5CFF]/15 text-[#C4B5FD]",
                ].join(" ")}
              >
                {createdJob?.status ?? submitState}
              </span>
            </div>

            <div className="mt-4 space-y-3 text-sm text-[#A5B4CC]">
              <p>
                Upload file_id:{" "}
                <span className="break-all text-[#F3F4F6]">
                  {uploadedFile ? String(uploadedFile.file_id) : "—"}
                </span>
              </p>
              <p>
                Job id:{" "}
                <span className="break-all text-[#F3F4F6]">
                  {createdJob ? String(createdJob.job_id) : "—"}
                </span>
              </p>
            </div>
          </div>
        </section>

        <section className="rounded-[18px] border border-white/10 bg-[#121A2B] p-5">
          <SectionHeading
            eyebrow="Подсказки"
            title="Что произойдёт дальше"
            description="После постановки задачи в очередь backend и worker выполнят цепочку этапов автоматически."
          />

          <ol className="mt-5 space-y-3 text-sm text-[#A5B4CC]">
            {[
              "Видео загружается и проходит серверную валидацию.",
              "Создаётся job с пресетом и merged runtime settings.",
              "Worker запускает стадии анализа, нарезки и улучшения.",
              "Прогресс публикуется через WebSocket в реальном времени.",
              "После завершения станут доступны результат, превью и скачивание.",
            ].map((item, index) => (
              <li key={item} className="flex gap-3">
                <span className="inline-flex h-6 w-6 flex-none items-center justify-center rounded-full bg-white/5 text-xs font-semibold text-[#F3F4F6]">
                  {index + 1}
                </span>
                <span className="leading-6">{item}</span>
              </li>
            ))}
          </ol>
        </section>
      </aside>
    </div>
  );
}

type SectionHeadingProps = {
  eyebrow: string;
  title: string;
  description: string;
};

/**
 * Standardized section heading used across the editor screen.
 */
function SectionHeading({ eyebrow, title, description }: SectionHeadingProps) {
  return (
    <div>
      <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
        {eyebrow}
      </p>
      <h2 className="mt-2 text-xl font-semibold text-[#F3F4F6]">{title}</h2>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-[#A5B4CC]">
        {description}
      </p>
    </div>
  );
}

type InfoChipProps = {
  label: string;
  value: string;
};

/**
 * Small informational chip used in the upload card.
 */
function InfoChip({ label, value }: InfoChipProps) {
  return (
    <div className="rounded-2xl border border-white/10 bg-[#101726] px-4 py-3">
      <div className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
        {label}
      </div>
      <div className="mt-1 text-sm font-medium text-[#F3F4F6]">{value}</div>
    </div>
  );
}

type FieldBlockProps = {
  label: string;
  description: string;
  children: ReactNode;
};

/**
 * Wrapper for an advanced settings form field group.
 */
function FieldBlock({ label, description, children }: FieldBlockProps) {
  return (
    <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4">
      <div className="mb-4">
        <h3 className="text-sm font-semibold text-[#F3F4F6]">{label}</h3>
        <p className="mt-1 text-sm leading-6 text-[#A5B4CC]">{description}</p>
      </div>
      {children}
    </div>
  );
}

type RangeControlProps = {
  min: number;
  max: number;
  step: number;
  value: number;
  displayValue: string;
  onChange: (value: number) => void;
};

/**
 * Shared range slider with a value badge.
 */
function RangeControl({
  min,
  max,
  step,
  value,
  displayValue,
  onChange,
}: RangeControlProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
          Значение
        </span>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs font-medium text-[#F3F4F6]">
          {displayValue}
        </span>
      </div>

      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-2 w-full cursor-pointer appearance-none rounded-full bg-white/10 accent-[#7C5CFF]"
      />

      <div className="flex items-center justify-between text-xs text-[#A5B4CC]">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

type ToggleCardProps = {
  checked: boolean;
  onChange: (value: boolean) => void;
  enabledLabel: string;
  disabledLabel: string;
};

/**
 * Compact toggle button with an explicit current state label.
 */
function ToggleCard({
  checked,
  onChange,
  enabledLabel,
  disabledLabel,
}: ToggleCardProps) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between rounded-2xl border border-white/10 bg-[#101726] px-4 py-3 text-left transition hover:bg-[#132038]"
      aria-pressed={checked}
    >
      <span className="text-sm text-[#F3F4F6]">
        {checked ? enabledLabel : disabledLabel}
      </span>

      <span
        className={[
          "relative inline-flex h-7 w-12 items-center rounded-full transition",
          checked ? "bg-[#7C5CFF]" : "bg-white/10",
        ].join(" ")}
      >
        <span
          className={[
            "inline-block h-5 w-5 transform rounded-full bg-white transition",
            checked ? "translate-x-6" : "translate-x-1",
          ].join(" ")}
        />
      </span>
    </button>
  );
}

type SelectControlOption = {
  label: string;
  value: string;
};

type SelectControlProps = {
  value: string;
  onChange: (value: string) => void;
  options: SelectControlOption[];
};

/**
 * Reusable native select to keep the implementation dependency-free.
 */
function SelectControl({ value, onChange, options }: SelectControlProps) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="w-full rounded-2xl border border-white/10 bg-[#101726] px-4 py-3 text-sm text-[#F3F4F6] outline-none transition focus:border-[#7C5CFF]"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

type StatusMessageProps = {
  kind: "error" | "warning" | "success";
  children: ReactNode;
  className?: string;
};

/**
 * Unified message box for inline feedback.
 */
function StatusMessage({
  kind,
  children,
  className,
}: StatusMessageProps) {
  const styles =
    kind === "error"
      ? "border-[#EF4444]/30 bg-[#EF4444]/10 text-[#FCA5A5]"
      : kind === "warning"
        ? "border-[#F59E0B]/30 bg-[#F59E0B]/10 text-[#FCD34D]"
        : "border-[#22C55E]/30 bg-[#22C55E]/10 text-[#86EFAC]";

  return (
    <div
      className={[
        "rounded-2xl border px-4 py-3 text-sm leading-6",
        styles,
        className ?? "",
      ].join(" ")}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}

type SummaryRowProps = {
  label: string;
  value: string;
};

/**
 * Simple two-column summary row used in the sidebar.
 */
function SummaryRow({ label, value }: SummaryRowProps) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-white/5 pb-3 last:border-b-0 last:pb-0">
      <dt className="text-sm text-[#A5B4CC]">{label}</dt>
      <dd className="max-w-[60%] break-words text-right text-sm font-medium text-[#F3F4F6]">
        {value}
      </dd>
    </div>
  );
}