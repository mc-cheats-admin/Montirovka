# AutoEdit

AutoEdit — это self-hosted веб-сервис для автоматического видеомонтажа и постобработки без использования внешних облачных API. Сервис принимает загруженное пользователем сырое видео, создаёт задание на обработку, выполняет тяжёлые этапы асинхронно через очередь задач, передаёт прогресс в реальном времени через WebSocket и предоставляет готовый результат для скачивания.

Проект ориентирован на приватную локальную обработку на собственном сервере с использованием FFmpeg, OpenCV, локальных ML-моделей и стандартной инфраструктуры FastAPI + Celery + Redis + PostgreSQL + Next.js.

## Возможности

- загрузка пользовательского видеофайла;
- выбор одного из готовых пресетов обработки:
  - Gaming / Highlight;
  - Tutorial / Обучение;
  - Cinematic / Контент;
- настройка параметров обработки:
  - целевой FPS;
  - интенсивность jump-cut;
  - zoom;
  - шумоподавление;
  - субтитры;
  - aspect ratio;
  - codec;
- асинхронная обработка через очередь Celery;
- realtime-обновление статуса задачи через WebSocket;
- анализ медиа через FFmpeg / ffprobe;
- локальная файловая система как основной storage;
- опциональная совместимость с S3/MinIO-режимом по env-настройкам;
- генерация итогового видео;
- выдача файла для скачивания;
- подготовка preview-артефактов;
- базовое тестовое покрытие backend-части.

## Для кого нужен AutoEdit

AutoEdit подходит для следующих сценариев:

- монтаж игровых хайлайтов;
- автоматическая чистка обучающих роликов;
- обработка влогов и контентных видео;
- локальный self-hosted видеопайплайн для небольших команд и студий;
- приватная обработка видео без отправки данных сторонним сервисам.

## Текущий стек

### Frontend

- Node.js 20.x
- Next.js 14
- React 18
- TypeScript 5
- Tailwind CSS 3

### Backend

- Python 3.11
- FastAPI
- SQLAlchemy 2.x
- Alembic
- Celery 5
- Redis
- PostgreSQL 16

### Видео, аудио и локальные инструменты

- FFmpeg
- ffprobe
- OpenCV
- SoX
- локальные модели и бинарники для:
  - Whisper
  - RIFE
  - Real-ESRGAN
  - RNNoise

## Архитектура

Сервис разделён на несколько логических частей:

- frontend — веб-интерфейс на Next.js;
- backend API — FastAPI-приложение с REST и WebSocket;
- worker — Celery worker для тяжёлой асинхронной обработки;
- postgres — хранение метаданных задач и файлов;
- redis — брокер Celery и канал прогресса;
- storage — локальное файловое хранилище по умолчанию.

Поток работы выглядит так:

1. Пользователь открывает веб-интерфейс.
2. Загружает файл через frontend.
3. Frontend вызывает backend endpoint загрузки.
4. Backend сохраняет файл и метаданные.
5. Frontend создаёт job с выбранным пресетом и настройками.
6. Backend ставит задачу в очередь Celery.
7. Worker выполняет пайплайн обработки.
8. Прогресс публикуется в Redis и транслируется в WebSocket.
9. Frontend показывает пользователю текущий этап и процент готовности.
10. После завершения пользователю становится доступен результат.

## Пресеты

### Gaming / Highlight

Сценарий для динамичного геймплея и хайлайтов.

Ключевые идеи:
- высокий FPS;
- динамичная нарезка;
- акцент на ярких игровых событиях;
- возможное использование highlight detection;
- упор на энергичную подачу материала.

Файл пресета:
- backend/app/presets/gaming.json

### Tutorial / Обучение

Сценарий для образовательных роликов, объяснений, talking-head и screen capture-видео.

Ключевые идеи:
- агрессивное удаление пауз;
- чистка речи;
- подготовка субтитров;
- более плавный и понятный монтаж.

Файл пресета:
- backend/app/presets/tutorial.json

### Cinematic / Контент

Сценарий для влогов, reels, shorts и контентных видео.

Ключевые идеи:
- мягкий монтаж;
- цветокоррекция;
- filmic feel;
- более кинематографичная подача.

Файл пресета:
- backend/app/presets/cinematic.json

## Основные директории проекта

Текущая структура репозитория организована в формате frontend/backend-монорепозитория.

### Корневой уровень

- .env.example — пример переменных окружения;
- docker-compose.yml — запуск всех сервисов;
- README.md — эта документация.

### Frontend

Расположен в директории frontend.

Ключевые файлы:
- frontend/app/layout.tsx — базовый layout приложения;
- frontend/app/page.tsx — главная страница;
- frontend/components/EditorClient.tsx — главный клиентский интерфейс редактора;
- frontend/components/JobStatusClient.tsx — клиент отслеживания статуса задачи;
- frontend/components/ResultViewer.tsx — просмотр результата;
- frontend/lib/api.ts — REST-клиент frontend;
- frontend/lib/ws.ts — WebSocket-клиент;
- frontend/lib/types.ts — общие TypeScript-типы;
- frontend/styles/globals.css — глобальные стили;
- frontend/package.json — зависимости и команды frontend.

### Backend

Расположен в директории backend.

Ключевые файлы:
- backend/app/main.py — точка входа FastAPI;
- backend/app/core/config.py — конфигурация через env;
- backend/app/core/logging.py — structured logging;
- backend/app/db/models.py — ORM-модели;
- backend/app/schemas.py — Pydantic-схемы;
- backend/app/api/routes.py — основные API-маршруты;
- backend/app/services/storage_service.py — работа с файлами;
- backend/app/services/job_service.py — создание и управление job;
- backend/app/services/preset_service.py — пресеты и merge настроек;
- backend/app/services/progress_service.py — публикация прогресса;
- backend/app/utils/media.py — утилиты обработки медиа;
- backend/app/workers/celery_app.py — Celery application;
- backend/app/workers/pipeline.py — coordinator пайплайна;
- backend/app/tests/test_api_and_services.py — базовые тесты;
- backend/requirements.txt — Python-зависимости;
- backend/alembic.ini и backend/alembic — миграции БД.

## Требования к окружению

Минимально для запуска проекта нужны:

- Docker Desktop или Docker Engine + Docker Compose v2;
- достаточно свободного места на диске для:
  - входных видео;
  - временных файлов обработки;
  - финальных рендеров;
  - локальных моделей;
- для полной функциональности — системные бинарники и локальные модели.

Если вы не используете Docker, понадобятся:

- Python 3.11;
- Node.js 20;
- PostgreSQL 16;
- Redis 7;
- FFmpeg и ffprobe;
- SoX;
- дополнительные локальные бинарники/модели по необходимости.

## 🚀 Как запустить

Ниже приведён рекомендуемый способ запуска через Docker Compose.

### 1. Предварительные требования

Убедитесь, что у вас установлены:

- Docker;
- Docker Compose v2;
- Git;
- достаточно места на диске для каталогов data/storage, data/tmp, data/output, data/previews и data/models.

Для Windows 10/11 лучше использовать Docker Desktop с включённой поддержкой Linux containers.

### 2. Клонирование проекта

Скопируйте проект локально:

    git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ>
    cd <ИМЯ_ПАПКИ_ПРОЕКТА>

### 3. Подготовка env-файла

Создайте файл .env на основе примера:

    copy .env.example .env

Если вы используете PowerShell:

    Copy-Item .env.example .env

При необходимости отредактируйте значения в .env.

### 4. Создание директорий для данных

Если каталогов ещё нет, создайте их:

    mkdir data
    mkdir data\storage
    mkdir data\tmp
    mkdir data\output
    mkdir data\previews
    mkdir data\models

На Linux/macOS можно использовать:

    mkdir -p data/storage data/tmp data/output data/previews data/models

### 5. Сборка и запуск контейнеров

Основная команда запуска:

    docker compose up --build

Если хотите запустить сервисы в фоне:

    docker compose up --build -d

### 6. Ожидание инициализации

Во время первого запуска произойдёт:

- сборка образа backend;
- установка Python-зависимостей;
- сборка образа frontend;
- установка Node-зависимостей;
- запуск PostgreSQL и Redis;
- выполнение Alembic migrations;
- старт FastAPI;
- старт Celery worker;
- старт Next.js frontend.

### 7. Проверка доступности сервисов

После запуска откройте:

- frontend:
  - http://localhost:3000
- backend health endpoint:
  - http://localhost:8000/api/v1/health

Ожидаемый ответ health endpoint:

    {"status":"ok","app_name":"AutoEdit"}

### 8. Как пользоваться после запуска

1. Откройте http://localhost:3000
2. Выберите видеофайл.
3. Выберите пресет.
4. При необходимости измените advanced settings.
5. Нажмите кнопку запуска обработки.
6. После создания задания перейдите на страницу статуса.
7. Дождитесь завершения пайплайна.
8. Скачайте результат.

### 9. Остановка проекта

Для остановки контейнеров:

    docker compose down

Для остановки с удалением volumes:

    docker compose down -v

### 10. Повторная сборка после изменений

Если вы меняли зависимости или Docker-конфигурацию:

    docker compose up --build

### 11. Локальный запуск без Docker

Это расширенный вариант, если вы хотите запускать сервисы вручную.

Backend:

    cd backend
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    alembic upgrade head
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Worker:

    cd backend
    .venv\Scripts\activate
    celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo -Q video_jobs,maintenance

Frontend:

    cd frontend
    npm install
    npm run dev

Если вы запускаете вручную на Windows, убедитесь, что PostgreSQL, Redis, FFmpeg и другие инструменты доступны в системе отдельно.

## Переменные окружения

Основные переменные, используемые проектом:

- APP_NAME=AutoEdit
- APP_ENV=development
- API_HOST=0.0.0.0
- API_PORT=8000
- FRONTEND_PORT=3000
- DATABASE_URL=postgresql+psycopg://autoedit:autoedit@postgres:5432/autoedit
- REDIS_URL=redis://redis:6379/0
- STORAGE_MODE=local
- STORAGE_LOCAL_ROOT=/data/storage
- UPLOAD_MAX_SIZE_BYTES=2147483648
- ALLOWED_VIDEO_EXTENSIONS=.mp4,.mov,.avi,.mkv
- TEMP_DIR=/data/tmp
- OUTPUT_DIR=/data/output
- PREVIEW_DIR=/data/previews
- MODELS_DIR=/data/models
- FFMPEG_BINARY=ffmpeg
- FFPROBE_BINARY=ffprobe
- SOX_BINARY=sox
- RNNOISE_BINARY=/usr/local/bin/rnnoise_demo
- RIFE_BINARY=python
- RIFE_SCRIPT=/opt/models/rife/inference_video.py
- REALESRGAN_BINARY=/usr/local/bin/realesrgan-ncnn-vulkan
- WHISPER_MODEL=small
- ENABLE_GPU=true
- LOG_LEVEL=INFO
- CORS_ORIGINS=http://localhost:3000
- JOB_RETENTION_HOURS=48
- WEBSOCKET_PING_INTERVAL=20
- PRESET_DIR=/app/backend/app/presets

Актуальный шаблон значений находится в файле:
- .env.example

## Локальные модели и бинарники

Проект принципиально не использует внешние облачные API. Для полной функциональности нужно самостоятельно подготовить локальные инструменты.

### Куда класть модели

Рекомендуемая директория для локальных моделей:

- data/models

Если используются дополнительные пути, они должны быть согласованы с env-переменными:
- MODELS_DIR
- RIFE_SCRIPT
- WHISPER_MODEL
- REALESRGAN_BINARY
- RNNOISE_BINARY

### Что нужно для полной функциональности

#### Обязательно

- FFmpeg
- ffprobe

Без них базовая видеообработка невозможна.

#### Желательно

- SoX — для некоторых этапов аудиообработки;
- RNNoise binary — для более качественного шумоподавления;
- локальная Whisper model — для генерации субтитров;
- RIFE — для продвинутой интерполяции FPS;
- Real-ESRGAN — для upscale / enhancement.

### Где именно хранить модели

Типичный пример структуры:

- data/models/whisper
- data/models/rife
- data/models/realesrgan

Если конкретный бинарник или скрипт вызывается по абсолютному пути внутри контейнера, путь должен быть отражён в .env.

## Fallback-механизмы

Проект спроектирован так, чтобы часть функций могла работать с деградацией возможностей.

Примеры fallback-поведения:

- если RIFE недоступен, интерполяция может быть упрощена или заменена FFmpeg-подходом;
- если RNNoise недоступен, используется более простой шумодав;
- если Real-ESRGAN недоступен, upscale-ветка пропускается или заменяется менее продвинутым способом;
- если субтитры отключены, отсутствие Whisper не мешает завершению job;
- если продвинутый enhancement недоступен, основной pipeline всё равно может отрендерить результат при наличии базовых FFmpeg-инструментов.

Важно: без FFmpeg / ffprobe пайплайн работать корректно не сможет.

## Основные API endpoints

Ниже краткая сводка по API, на которые уже ориентируются frontend-клиенты.

### Health

GET /api/v1/health

Назначение:
- проверить, что backend запущен.

### Presets

GET /api/v1/presets

Назначение:
- получить список встроенных пресетов.

### Upload

POST /api/v1/uploads

Назначение:
- загрузить видеофайл.

Формат:
- multipart/form-data
- поле file

### Jobs

POST /api/v1/jobs

Назначение:
- создать новое задание обработки.

GET /api/v1/jobs/{job_id}

Назначение:
- получить состояние и детали задания.

DELETE /api/v1/jobs/{job_id}

Назначение:
- отменить или очистить задание.

### Realtime events

WebSocket:
- /api/v1/jobs/{job_id}/events

Назначение:
- получать прогресс задачи в реальном времени.

### Result download

GET /api/v1/jobs/{job_id}/download

Назначение:
- скачать финальный видеофайл.

### Media access

GET /api/v1/results/media/{file_id}

Назначение:
- скачать или открыть отдельный media-артефакт.

## Frontend-часть

Frontend уже использует следующие модули:

- frontend/components/EditorClient.tsx
- frontend/components/JobStatusClient.tsx
- frontend/components/ResultViewer.tsx
- frontend/lib/api.ts
- frontend/lib/ws.ts
- frontend/lib/types.ts

### Что умеет frontend сейчас

- загружать файл;
- загружать список пресетов;
- создавать job;
- отслеживать job через REST + WebSocket;
- показывать текущий stage и прогресс;
- отображать результат и ссылки на скачивание.

### Дизайн

Интерфейс выполнен в тёмной минималистичной стилистике:

- основной фон: #0B1020
- поверхностные карточки: #121A2B
- вторичные блоки: #182235
- акценты: #7C5CFF и #00C2FF

Стили определены в:
- frontend/styles/globals.css
- frontend/tailwind.config.ts

## Backend-часть

Backend организован вокруг FastAPI и сервисного слоя.

### Что делает backend

- принимает загрузки;
- валидирует входные данные;
- создаёт задания;
- хранит метаданные в PostgreSQL;
- публикует прогресс;
- подготавливает данные для worker-пайплайна;
- отдаёт итоговые файлы и медиа-артефакты.

### Worker

Worker работает отдельно от API-процесса и исполняет тяжёлые задачи.

Ключевые файлы:
- backend/app/workers/celery_app.py
- backend/app/workers/pipeline.py

Celery использует:
- Redis как broker;
- Redis как result backend;
- очередь video_jobs для обработки задач.

## База данных

Проект использует PostgreSQL для хранения:

- jobs;
- media_files;
- preset snapshots;
- сопутствующих метаданных обработки.

Миграции управляются через Alembic:

- backend/alembic.ini
- backend/alembic/env.py
- backend/alembic/versions/0001_initial.py

При запуске через docker compose backend автоматически выполняет:

    alembic upgrade head

## Хранилище файлов

По умолчанию используется локальная файловая система.

Основные каталоги:

- /data/storage
- /data/tmp
- /data/output
- /data/previews
- /data/models

В docker-compose эти каталоги смонтированы из локальной папки проекта:

- ./data/storage
- ./data/tmp
- ./data/output
- ./data/previews
- ./data/models

Опционально можно настроить режим S3-совместимого хранилища через env-переменные, но по умолчанию MinIO не поднимается.

## Логи

Проект ориентирован на structured logging.

Ожидаемые особенности:
- JSON-совместимый формат логов;
- логирование старта и завершения stage;
- логирование обновлений статуса задач;
- логирование subprocess-команд;
- отсутствие вывода бинарного содержимого файлов и секретов.

Ключевой модуль:
- backend/app/core/logging.py

## Тестирование

В текущем проекте есть backend-тесты:

- backend/app/tests/test_api_and_services.py

Для запуска тестов вручную:

    cd backend
    python -m pytest app/tests -q

Если вы используете Docker и хотите выполнить тесты в контейнере backend:

    docker compose run --rm backend pytest app/tests -q

Примечание: часть тяжёлых зависимостей и системных инструментов в unit-тестах обычно мокается или не требуется.

## Полезные команды

### Запуск всех сервисов

    docker compose up --build

### Запуск в фоне

    docker compose up --build -d

### Просмотр логов

    docker compose logs -f

### Логи только backend

    docker compose logs -f backend

### Логи только worker

    docker compose logs -f worker

### Остановка

    docker compose down

### Перезапуск backend

    docker compose restart backend

### Перезапуск worker

    docker compose restart worker

## Ограничения

На текущем этапе и по самой природе self-hosted-видеопроцессинга есть несколько важных ограничений:

- производительность зависит от CPU/GPU и скорости диска;
- большие файлы требуют много временного пространства;
- некоторые функции доступны только при наличии локальных моделей и бинарников;
- качество advanced-обработки зависит от подготовленности локального окружения;
- без GPU тяжёлые этапы могут работать существенно медленнее;
- realtime-статус зависит от корректной работы Redis и WebSocket-соединения;
- preview и subtitles являются дополнительными артефактами и могут зависеть от состояния конкретных этапов pipeline.

## Известные fallback-сценарии

Нормальные рабочие сценарии деградации возможностей:

- обработка может завершиться без субтитров, если они пользователем не запрашивались;
- upscale и advanced interpolation могут быть пропущены при отсутствии соответствующих инструментов;
- часть enhancement-логики может быть упрощена без полной остановки всей задачи;
- frontend продолжает получать состояние задачи через REST, даже если кратковременно теряется WebSocket.

## Безопасность и приватность

AutoEdit задуман как приватный self-hosted инструмент:

- пользовательские видео не отправляются во внешние API;
- все вычисления происходят в локальной инфраструктуре владельца сервиса;
- хранилище и БД находятся под вашим контролем;
- настройка доступа, reverse proxy и сетевой защиты должны выполняться на уровне вашей инфраструктуры.

## Что уже синхронизировано в текущем репозитории

README соответствует текущим именам и модулям, уже присутствующим в проекте, включая:

- frontend/app/layout.tsx
- frontend/app/page.tsx
- frontend/components/EditorClient.tsx
- frontend/components/JobStatusClient.tsx
- frontend/components/ResultViewer.tsx
- frontend/lib/api.ts
- frontend/lib/ws.ts
- frontend/lib/types.ts
- frontend/styles/globals.css
- backend/app/main.py
- backend/app/core/config.py
- backend/app/core/logging.py
- backend/app/db/models.py
- backend/app/schemas.py
- backend/app/api/routes.py
- backend/app/services/storage_service.py
- backend/app/services/job_service.py
- backend/app/services/preset_service.py
- backend/app/services/progress_service.py
- backend/app/utils/media.py
- backend/app/workers/celery_app.py
- backend/app/workers/pipeline.py
- backend/app/tests/test_api_and_services.py
- backend/requirements.txt
- docker-compose.yml
- .env.example

## Планируемое дальнейшее расширение

Согласно проектному плану, сервис может быть расширен дополнительными модулями:

- более детализированные backend routes;
- отдельные схемы и модели по доменным сущностям;
- расширенные worker stages;
- более полный preview pipeline;
- раздельные страницы jobs/[jobId] и results/[jobId];
- расширенный набор unit и integration tests.

## Рекомендации для production-развёртывания

Для production стоит дополнительно предусмотреть:

- reverse proxy перед frontend/backend;
- HTTPS;
- ограничение размера входящих запросов на proxy-уровне;
- мониторинг Redis/PostgreSQL;
- лог-ротацию;
- регулярную очистку временных файлов и retention policy;
- резервное копирование PostgreSQL;
- отдельное хранилище для data и output на быстром диске;
- GPU-конфигурацию, если нужны RIFE / Real-ESRGAN / ускоренные encode-пути.

## Краткий итог

AutoEdit — это автономная основа для локального сервиса автоматического видеомонтажа с современным web-интерфейсом, очередями задач, realtime-статусом и self-hosted-подходом. Даже в минимальной конфигурации проект уже задаёт единую архитектуру frontend + backend + worker + storage + queue, а при подключении локальных моделей превращается в полноценную платформу приватной видеообработки.

Если вы развиваете проект дальше, рекомендуется сохранять текущую архитектурную логику:
- frontend на Next.js;
- backend на FastAPI;
- асинхронная обработка через Celery;
- Redis для очереди и прогресса;
- PostgreSQL для метаданных;
- локальные инструменты для обработки медиа без внешних API.