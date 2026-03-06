import type { Metadata } from "next";
import EditorClient from "../components/EditorClient";

export const metadata: Metadata = {
  title: "Главная",
  description:
    "Загрузка видео, выбор пресета и запуск автоматического монтажа в AutoEdit.",
};

/**
 * Home page for the AutoEdit application.
 *
 * Design goals for this page:
 * - provide a clear entry point for the product;
 * - explain the value proposition in a few short blocks;
 * - host the main interactive editor/upload client component;
 * - keep the page itself server-rendered and lightweight;
 * - delegate all user interaction and browser APIs to EditorClient.
 *
 * The page intentionally contains only presentation and composition logic.
 * Stateful behavior such as file upload, presets loading, job creation and
 * status transitions is expected to live inside client-side components.
 */
export default function HomePage() {
  return (
    <main className="relative overflow-hidden">
      <BackgroundDecorations />

      <div className="mx-auto flex w-full max-w-7xl flex-col gap-8 px-4 py-6 sm:px-6 lg:px-8 lg:py-10">
        <HeroSection />

        <section
          aria-label="Редактор AutoEdit"
          className="rounded-[20px] border border-white/10 bg-white/5 p-3 shadow-[0_10px_30px_rgba(0,0,0,0.25)] backdrop-blur sm:p-4 lg:p-5"
        >
          <EditorClient />
        </section>

        <FeaturesSection />
        <HintsSection />
      </div>
    </main>
  );
}

/**
 * Decorative blurred gradients used across the landing/editor page.
 *
 * Keeping this in a small helper component makes the main page tree easier
 * to scan and avoids repeating long utility class strings inline.
 */
function BackgroundDecorations() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 overflow-hidden"
    >
      <div className="absolute left-[-8rem] top-[-4rem] h-72 w-72 rounded-full bg-[#7C5CFF]/20 blur-3xl" />
      <div className="absolute right-[-6rem] top-20 h-80 w-80 rounded-full bg-[#00C2FF]/15 blur-3xl" />
      <div className="absolute bottom-[-8rem] left-1/3 h-96 w-96 rounded-full bg-[#7C5CFF]/10 blur-3xl" />
    </div>
  );
}

/**
 * Top hero block with product positioning and key quick facts.
 */
function HeroSection() {
  return (
    <section className="grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)] lg:items-stretch">
      <div className="rounded-[20px] border border-white/10 bg-[#121A2B]/90 p-6 shadow-[0_10px_30px_rgba(0,0,0,0.25)] backdrop-blur sm:p-8">
        <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs font-medium tracking-wide text-[#A5B4CC]">
          <span aria-hidden="true">🎬</span>
          <span>Self-hosted AI Video Editor</span>
        </div>

        <h1 className="max-w-3xl text-3xl font-semibold tracking-tight text-[#F3F4F6] sm:text-4xl lg:text-5xl">
          AutoEdit — локальный сервис
          <span className="block bg-gradient-to-r from-[#7C5CFF] to-[#00C2FF] bg-clip-text text-transparent">
            автоматического видеомонтажа
          </span>
        </h1>

        <p className="mt-4 max-w-3xl text-sm leading-7 text-[#A5B4CC] sm:text-base">
          Загружайте исходное видео, выбирайте пресет и запускайте автономную
          обработку на собственном сервере. Очередь задач, FFmpeg, OpenCV,
          локальные ML-модели и прогресс в реальном времени — без внешних API и
          без передачи ваших медиа третьим сторонам.
        </p>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <HeroMetric
            title="До 2 GB"
            description="Загрузка крупных исходников напрямую в сервис"
          />
          <HeroMetric
            title="3 пресета"
            description="Gaming, Tutorial и Cinematic под разные сценарии"
          />
          <HeroMetric
            title="Realtime статус"
            description="Отслеживание этапов пайплайна через WebSocket"
          />
        </div>
      </div>

      <aside className="rounded-[20px] border border-white/10 bg-[#121A2B]/80 p-6 shadow-[0_10px_30px_rgba(0,0,0,0.25)] backdrop-blur sm:p-8">
        <h2 className="text-lg font-semibold text-[#F3F4F6]">
          Что умеет пайплайн AutoEdit
        </h2>

        <ul className="mt-5 space-y-4 text-sm text-[#A5B4CC]">
          <FeatureBullet
            title="Умная нарезка"
            description="Автоматическое удаление тишины, пауз и слабых фрагментов по анализу аудио и видео."
          />
          <FeatureBullet
            title="Локальная обработка"
            description="Повышение качества, стабилизация, нормализация аудио и интерполяция FPS без облачных API."
          />
          <FeatureBullet
            title="Пресеты монтажа"
            description="Готовые сценарии для геймплея, обучающих роликов и cinematic-контента."
          />
          <FeatureBullet
            title="Превью и скачивание"
            description="После завершения вы получаете итоговый файл, превью и при необходимости субтитры."
          />
        </ul>

        <div className="mt-6 rounded-2xl border border-white/10 bg-[#182235] p-4">
          <p className="text-xs uppercase tracking-[0.2em] text-[#A5B4CC]">
            Статусы пайплайна
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {[
              "queued",
              "analyzing",
              "cutting",
              "enhancing",
              "interpolating",
              "processing_audio",
              "rendering",
            ].map((status) => (
              <span
                key={status}
                className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-[#F3F4F6]"
              >
                {status}
              </span>
            ))}
          </div>
        </div>
      </aside>
    </section>
  );
}

type HeroMetricProps = {
  title: string;
  description: string;
};

/**
 * Small metric/info card displayed inside the hero section.
 */
function HeroMetric({ title, description }: HeroMetricProps) {
  return (
    <div className="rounded-2xl border border-white/10 bg-[#182235]/80 p-4">
      <div className="text-lg font-semibold text-[#F3F4F6]">{title}</div>
      <p className="mt-1 text-sm leading-6 text-[#A5B4CC]">{description}</p>
    </div>
  );
}

type FeatureBulletProps = {
  title: string;
  description: string;
};

/**
 * Reusable item for short descriptive bullets in the sidebar.
 */
function FeatureBullet({ title, description }: FeatureBulletProps) {
  return (
    <li className="flex gap-3">
      <span
        aria-hidden="true"
        className="mt-1 inline-flex h-5 w-5 flex-none items-center justify-center rounded-full bg-gradient-to-br from-[#7C5CFF] to-[#00C2FF] text-[10px] font-bold text-white"
      >
        ✓
      </span>
      <div>
        <div className="font-medium text-[#F3F4F6]">{title}</div>
        <p className="mt-1 leading-6 text-[#A5B4CC]">{description}</p>
      </div>
    </li>
  );
}

/**
 * Product feature cards under the main editor block.
 *
 * This section supports the editor area with concise explanation of
 * user-facing behavior and expected workflow.
 */
function FeaturesSection() {
  const items = [
    {
      title: "Gaming / Highlight",
      description:
        "Динамичная нарезка, поиск хайлайтов, высокий FPS и акцент на ярких игровых моментах.",
    },
    {
      title: "Tutorial / Обучение",
      description:
        "Агрессивное удаление пауз, усиление речи, субтитры и более чистая подача материала.",
    },
    {
      title: "Cinematic / Контент",
      description:
        "Мягкая обработка, цветокоррекция, filmic-подача и аккуратный итоговый рендер.",
    },
  ];

  return (
    <section
      aria-label="Преимущества и сценарии"
      className="grid gap-4 lg:grid-cols-3"
    >
      {items.map((item) => (
        <article
          key={item.title}
          className="rounded-[18px] border border-white/10 bg-[#121A2B]/85 p-5 shadow-[0_10px_30px_rgba(0,0,0,0.2)]"
        >
          <h2 className="text-base font-semibold text-[#F3F4F6]">
            {item.title}
          </h2>
          <p className="mt-3 text-sm leading-7 text-[#A5B4CC]">
            {item.description}
          </p>
        </article>
      ))}
    </section>
  );
}

/**
 * Bottom hint panel with practical usage constraints.
 *
 * These hints mirror backend validation rules and help users avoid
 * unnecessary failed uploads before interacting with the form.
 */
function HintsSection() {
  const hints = [
    "Поддерживаемые форматы: .mp4, .mov, .avi, .mkv",
    "Максимальный размер файла: 2 GB",
    "Обработка выполняется асинхронно — можно открыть страницу статуса задачи",
    "Для субтитров и advanced enhancement требуются локальные модели и системные бинарники",
  ];

  return (
    <section className="rounded-[20px] border border-white/10 bg-[#121A2B]/80 p-6 shadow-[0_10px_30px_rgba(0,0,0,0.2)] sm:p-8">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-[#F3F4F6]">
            Подсказки перед запуском
          </h2>
          <p className="mt-1 text-sm text-[#A5B4CC]">
            Эти правила помогут быстрее пройти валидацию и успешно поставить
            видео в очередь.
          </p>
        </div>

        <div className="inline-flex rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-[#A5B4CC]">
          FastAPI + Celery + Redis + FFmpeg + OpenCV
        </div>
      </div>

      <ul className="mt-5 grid gap-3 md:grid-cols-2">
        {hints.map((hint) => (
          <li
            key={hint}
            className="rounded-2xl border border-white/10 bg-[#182235]/70 px-4 py-3 text-sm leading-6 text-[#A5B4CC]"
          >
            {hint}
          </li>
        ))}
      </ul>
    </section>
  );
}