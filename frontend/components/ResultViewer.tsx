import Link from "next/link";

type ResultViewerProps = {
  jobId: string;
  beforeUrl: string | null;
  afterUrl: string | null;
  thumbnailUrl: string | null;
  downloadUrl: string;
  subtitleUrl?: string | null;
  presetName?: string | null;
  outputFilename?: string | null;
  subtitleFilename?: string | null;
  completedAt?: string | null;
  analysis?: Record<string, unknown> | null;
  className?: string;
};

/**
 * Translates internal preset names into user-facing Russian labels.
 *
 * The component accepts a raw preset name from the API and converts it into
 * a stable UI label. Unknown values are still displayed safely.
 */
function humanizePresetName(presetName: string | null | undefined): string {
  switch (presetName) {
    case "gaming":
      return "Gaming / Highlight";
    case "tutorial":
      return "Tutorial / Обучение";
    case "cinematic":
      return "Cinematic / Контент";
    default:
      return presetName ?? "Не указан";
  }
}

/**
 * Formats an ISO datetime string for compact Russian UI output.
 */
function formatDateTime(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

/**
 * Formats duration values from analysis metadata into a readable string.
 */
function formatDuration(seconds: unknown): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }

  if (seconds < 60) {
    return `${seconds.toFixed(1)} сек`;
  }

  const wholeSeconds = Math.round(seconds);
  const minutes = Math.floor(wholeSeconds / 60);
  const remainingSeconds = wholeSeconds % 60;

  return `${minutes} мин ${remainingSeconds} сек`;
}

/**
 * Builds compact summary items from optional analysis data.
 *
 * This viewer does not require analysis to be present, but if the result page
 * already has it from the backend, the component can show a concise technical
 * summary without extra requests.
 */
function buildAnalysisSummary(
  analysis: Record<string, unknown> | null | undefined,
): Array<{ label: string; value: string }> {
  if (!analysis) {
    return [];
  }

  const items: Array<{ label: string; value: string }> = [];

  if (
    typeof analysis.width === "number" &&
    typeof analysis.height === "number"
  ) {
    items.push({
      label: "Исходное разрешение",
      value: `${analysis.width}×${analysis.height}`,
    });
  }

  if (typeof analysis.fps === "number" && Number.isFinite(analysis.fps)) {
    items.push({
      label: "FPS исходника",
      value: `${analysis.fps}`,
    });
  }

  const durationValue = formatDuration(analysis.duration_seconds);
  if (durationValue) {
    items.push({
      label: "Длительность",
      value: durationValue,
    });
  }

  if (
    Array.isArray(analysis.silence_segments) &&
    typeof analysis.silence_segments.length === "number"
  ) {
    items.push({
      label: "Сегменты тишины",
      value: `${analysis.silence_segments.length}`,
    });
  }

  if (
    Array.isArray(analysis.scene_changes) &&
    typeof analysis.scene_changes.length === "number"
  ) {
    items.push({
      label: "Найдено смен сцен",
      value: `${analysis.scene_changes.length}`,
    });
  }

  return items;
}

/**
 * Lightweight utility to join static class names without adding an external dependency.
 */
function joinClassNames(...parts: Array<string | null | undefined | false>): string {
  return parts.filter(Boolean).join(" ");
}

/**
 * ResultViewer displays the final output section for a completed AutoEdit job.
 *
 * Responsibilities:
 * - show before/after preview players if preview media is available;
 * - expose direct actions for downloading the final file and subtitles;
 * - render concise technical metadata about the completed job;
 * - keep the UI useful even if preview assets are not ready yet.
 *
 * The component is intentionally presentational and does not perform network
 * requests by itself. Data should be prepared by the calling page or parent
 * client component.
 */
export default function ResultViewer({
  jobId,
  beforeUrl,
  afterUrl,
  thumbnailUrl,
  downloadUrl,
  subtitleUrl = null,
  presetName = null,
  outputFilename = null,
  subtitleFilename = null,
  completedAt = null,
  analysis = null,
  className,
}: ResultViewerProps) {
  const analysisSummary = buildAnalysisSummary(analysis);
  const completedAtLabel = formatDateTime(completedAt);
  const hasAnyPreview = Boolean(beforeUrl || afterUrl);

  return (
    <section
      aria-label="Результат обработки"
      className={joinClassNames(
        "rounded-[20px] border border-white/10 bg-[#121A2B] p-5 shadow-[0_10px_30px_rgba(0,0,0,0.25)] sm:p-6",
        className,
      )}
    >
      <div className="flex flex-col gap-4 border-b border-white/10 pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
            Результат AutoEdit
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight text-[#F3F4F6] sm:text-3xl">
            Готовое видео
          </h2>
          <p className="mt-2 max-w-3xl text-sm leading-7 text-[#A5B4CC]">
            Задача завершена. Ниже доступны превью до/после, итоговый файл для
            скачивания и дополнительные артефакты обработки.
          </p>
        </div>

        <div className="inline-flex items-center gap-2 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-3 py-1.5 text-sm font-medium text-emerald-300">
          <span aria-hidden="true">●</span>
          <span>Готово</span>
        </div>
      </div>

      <div className="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <InfoCard label="Job ID" value={jobId} mono />
        <InfoCard label="Пресет" value={humanizePresetName(presetName)} />
        <InfoCard
          label="Завершено"
          value={completedAtLabel ?? "Недавно"}
        />
        <InfoCard
          label="Итоговый файл"
          value={outputFilename ?? "autoedit-result.mp4"}
        />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold text-[#F3F4F6]">
                Превью результата
              </h3>
              <p className="mt-1 text-sm text-[#A5B4CC]">
                Сравните исходный и обработанный фрагменты.
              </p>
            </div>

            {hasAnyPreview ? (
              <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-[#A5B4CC]">
                before / after
              </span>
            ) : (
              <span className="rounded-full border border-amber-400/20 bg-amber-400/10 px-3 py-1 text-xs text-amber-300">
                Превью ещё недоступно
              </span>
            )}
          </div>

          <div className="mt-5 grid gap-4 lg:grid-cols-2">
            <VideoPanel
              title="До"
              description="Фрагмент исходного видео"
              src={beforeUrl}
              poster={thumbnailUrl}
              emptyMessage="Исходное превью недоступно."
            />

            <VideoPanel
              title="После"
              description="Результат автоматической обработки"
              src={afterUrl}
              poster={thumbnailUrl}
              emptyMessage="Итоговое превью недоступно."
              highlight
            />
          </div>
        </div>

        <aside className="flex flex-col gap-4">
          <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
            <h3 className="text-base font-semibold text-[#F3F4F6]">
              Действия
            </h3>
            <p className="mt-1 text-sm leading-6 text-[#A5B4CC]">
              Скачайте итоговый рендер и, если они были сгенерированы, файлы
              субтитров.
            </p>

            <div className="mt-4 flex flex-col gap-3">
              <a
                href={downloadUrl}
                className="inline-flex items-center justify-center rounded-xl bg-gradient-to-r from-[#7C5CFF] to-[#00C2FF] px-4 py-3 text-sm font-semibold text-white shadow-[0_10px_30px_rgba(124,92,255,0.25)] transition hover:opacity-95 focus:outline-none focus:ring-2 focus:ring-[#7C5CFF]/60"
              >
                Скачать итоговое видео
              </a>

              {subtitleUrl ? (
                <a
                  href={subtitleUrl}
                  className="inline-flex items-center justify-center rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-[#F3F4F6] transition hover:bg-white/10 focus:outline-none focus:ring-2 focus:ring-white/20"
                >
                  Скачать субтитры
                  {subtitleFilename ? ` (${subtitleFilename})` : ""}
                </a>
              ) : (
                <div className="rounded-xl border border-white/10 bg-black/10 px-4 py-3 text-sm text-[#A5B4CC]">
                  Субтитры для этой задачи не были созданы.
                </div>
              )}

              <Link
                href="/"
                className="inline-flex items-center justify-center rounded-xl border border-white/10 bg-transparent px-4 py-3 text-sm font-medium text-[#A5B4CC] transition hover:bg-white/5 hover:text-[#F3F4F6] focus:outline-none focus:ring-2 focus:ring-white/20"
              >
                Обработать новое видео
              </Link>
            </div>
          </div>

          <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
            <h3 className="text-base font-semibold text-[#F3F4F6]">
              О результате
            </h3>

            <dl className="mt-4 space-y-3">
              <MetaRow label="Статус" value="completed" success />
              <MetaRow label="Preset" value={humanizePresetName(presetName)} />
              <MetaRow
                label="Видео"
                value={outputFilename ?? "autoedit-result.mp4"}
              />
              <MetaRow
                label="Субтитры"
                value={subtitleFilename ?? (subtitleUrl ? "subtitle.srt" : "Нет")}
              />
            </dl>
          </div>
        </aside>
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
          <h3 className="text-base font-semibold text-[#F3F4F6]">
            Что дальше
          </h3>
          <ul className="mt-4 space-y-3 text-sm leading-7 text-[#A5B4CC]">
            <li className="flex gap-3">
              <span className="mt-1 text-[#22C55E]">✓</span>
              <span>Проверьте итоговый рендер в локальном плеере после скачивания.</span>
            </li>
            <li className="flex gap-3">
              <span className="mt-1 text-[#22C55E]">✓</span>
              <span>Сохраните subtitle sidecar отдельно, если планируете публикацию с субтитрами.</span>
            </li>
            <li className="flex gap-3">
              <span className="mt-1 text-[#22C55E]">✓</span>
              <span>При необходимости запустите повторную обработку с другим пресетом или настройками.</span>
            </li>
          </ul>
        </div>

        <div className="rounded-[18px] border border-white/10 bg-[#182235] p-4 sm:p-5">
          <h3 className="text-base font-semibold text-[#F3F4F6]">
            Сводка анализа
          </h3>

          {analysisSummary.length > 0 ? (
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {analysisSummary.map((item) => (
                <div
                  key={item.label}
                  className="rounded-2xl border border-white/10 bg-black/10 p-4"
                >
                  <div className="text-xs uppercase tracking-[0.16em] text-[#A5B4CC]">
                    {item.label}
                  </div>
                  <div className="mt-2 text-sm font-medium text-[#F3F4F6]">
                    {item.value}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-4 rounded-2xl border border-white/10 bg-black/10 p-4 text-sm leading-6 text-[#A5B4CC]">
              Подробная аналитика исходного файла недоступна, но итоговый результат
              уже успешно подготовлен и готов к скачиванию.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

type InfoCardProps = {
  label: string;
  value: string;
  mono?: boolean;
};

/**
 * Compact reusable info card matching the visual language of the existing pages.
 */
function InfoCard({ label, value, mono = false }: InfoCardProps) {
  return (
    <div className="rounded-2xl border border-white/10 bg-[#182235] p-4">
      <div className="text-xs uppercase tracking-[0.16em] text-[#A5B4CC]">
        {label}
      </div>
      <div
        className={joinClassNames(
          "mt-2 break-all text-sm font-medium text-[#F3F4F6]",
          mono && "font-mono text-xs sm:text-sm",
        )}
      >
        {value}
      </div>
    </div>
  );
}

type MetaRowProps = {
  label: string;
  value: string;
  success?: boolean;
};

/**
 * Metadata row used in the side panel.
 */
function MetaRow({ label, value, success = false }: MetaRowProps) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-white/5 pb-3 last:border-b-0 last:pb-0">
      <dt className="text-sm text-[#A5B4CC]">{label}</dt>
      <dd
        className={joinClassNames(
          "text-right text-sm font-medium",
          success ? "text-[#22C55E]" : "text-[#F3F4F6]",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

type VideoPanelProps = {
  title: string;
  description: string;
  src: string | null;
  poster: string | null;
  emptyMessage: string;
  highlight?: boolean;
};

/**
 * Dedicated preview panel for before/after video blocks.
 *
 * The panel is intentionally resilient:
 * - if src exists, it renders a standard HTML5 video player;
 * - if src is absent, it renders an informative empty state instead of failing;
 * - poster is optional and reused as a thumbnail for both players.
 */
function VideoPanel({
  title,
  description,
  src,
  poster,
  emptyMessage,
  highlight = false,
}: VideoPanelProps) {
  return (
    <div
      className={joinClassNames(
        "overflow-hidden rounded-[18px] border bg-[#121A2B]",
        highlight
          ? "border-[#7C5CFF]/30 shadow-[0_10px_30px_rgba(124,92,255,0.12)]"
          : "border-white/10",
      )}
    >
      <div className="flex items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
        <div>
          <h4 className="text-sm font-semibold text-[#F3F4F6]">{title}</h4>
          <p className="mt-1 text-xs leading-5 text-[#A5B4CC]">{description}</p>
        </div>

        {highlight ? (
          <span className="rounded-full border border-[#7C5CFF]/30 bg-[#7C5CFF]/10 px-2.5 py-1 text-[11px] font-medium text-[#C4B5FD]">
            processed
          </span>
        ) : (
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] font-medium text-[#A5B4CC]">
            source
          </span>
        )}
      </div>

      <div className="aspect-video bg-black">
        {src ? (
          <video
            controls
            preload="metadata"
            playsInline
            poster={poster ?? undefined}
            className="h-full w-full bg-black object-contain"
          >
            <source src={src} />
            Ваш браузер не поддерживает встроенное воспроизведение видео.
          </video>
        ) : (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm leading-6 text-[#A5B4CC]">
            {emptyMessage}
          </div>
        )}
      </div>
    </div>
  );
}