# SCS-001 Learning — Submission Read, Teacher Submission Discovery, Review и Learning Content Authoring (Slices 1–4)

**Класс:** A — Business System / SCS
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.2.0
**Владелец данных:** `learning` (логическая схема `learning`)
**Задача:** SCS-001 Slice 1 (prerequisite) + Slice 2 — Issue #20 + Slice 3 — Issue #24 + Slice 4 — Issue #31
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §5.3, §5.4, §6.1, §6.2,
§6.5, §26, §27, LAW-03, LAW-04, LAW-16, LAW-16a; ADR-0011

SCS-001 — бизнес-компонент, владелец учебных данных. Срез 1 публикует чтение
отдельной работы, срез 2 добавляет teacher discovery — список работ задания,
срез 3 добавляет ровно одну команду изменения состояния — review работы
учителем, срез 4 добавляет иерархию содержания над существующим потоком —
Course → Module → Lesson → Assignment (Issue #31):

```text
Subject → Submission read (срез 1)
Teacher → List one Assignment's submissions (срез 2)
Teacher → Record review of one Submission (срез 3)
Teacher → Create Course / Module / Lesson / Assignment (срез 4)
Teacher → Publish Course (атомарно, вся иерархия) (срез 4)
Teacher → Archive Course (атомарно, вся иерархия) (срез 4)
Student → Read published Course hierarchy (срез 4)
```

Это не grading-платформа, не analytics, не progress-трекинг, не LMS с
версионированием и не generic CRUD: компонент владеет учебными данными
(курсы, модули, уроки, задания, работы) и ровно одной границей контроля.

## Семантика review

Review означает ровно одно: учитель фиксирует факт, что конкретная Submission
проверена. Review — это **не** оценка, не балл, не grading, не feedback, не
комментарий, не evaluation и не LearningResult. Review не меняет lifecycle
Submission и не создаёт сущности Review.

Факт review — **однократный и неизменяемый (immutable)**: `reviewed_by` и
`reviewed_at` устанавливаются первым успешным review и далее не
перезаписываются. Повтор с тем же `Idempotency-Key` — это идемпотентный
replay (тот же результат, без второго эффекта); повтор с **другим**
`Idempotency-Key` — новая команда, которая отказывается с `ALREADY_REVIEWED`
и не меняет первый факт review.

## Семантика content authoring (срез 4)

Иерархия `Course → Module → Lesson → Assignment` строится **над** существующим
потоком Submission → Teacher Review и не меняет его семантики. Assignment —
это существующая модель, минимально расширенная полями связи с иерархией:
`lesson_id`, `title`, `instructions`. Идентичность Assignment и поток работ
сохраняются без изменений.

- **Каждый ребёнок принадлежит ровно одному родителю одного и того же
  тенанта.** Сирота (orphan) или ребёнок чужого тенанта невозможны: хранилище
  проверяет это при регистрации, команда — при создании, публикация — заново
  перед первой записью (defence in depth).
- **Новая сущность создаётся только в `DRAFT`** и только под родителем в
  `DRAFT`. Никакого редактирования, перемещения, reorder и версионирования в
  компоненте нет: PUT/PATCH/DELETE отсутствуют как класс.
- **Публикация — команда уровня Course и она атомарна.** Команда проверяет
  структурную валидность всей иерархии (принадлежность, тенант, `DRAFT`-состоя
  всех потомков) и только затем переводит Course и всех потомков
  `DRAFT → PUBLISHED` одним эффектом. Отказ валидации или зависимости не
  публикует ничего. Опубликованная иерархия неизменяема; `UNPUBLISH` не
  существует.
- **Архив — команда уровня Course, только из `PUBLISHED` в `ARCHIVED`,
  атомарно для всей иерархии.** `UNARCHIVE` не существует.
- **Student читает только опубликованные иерархии своего тенанта.** Вопрос
  IS-003 выбирается собственным сохранённым состоянием Course:
  `learning.courses.read` для `PUBLISHED` (учитель и студент),
  `learning.courses.read_unpublished` для остальных (учитель). Archived и
  draft-содержимое студенту недоступно.

Все шесть команд авторинга требуют `Idempotency-Key` и исполняются через
IS-005: точный replay возвращает записанный результат без второго эффекта,
изменение binding (payload, identity, тенант, операция, цель) —
`IDEMPOTENCY_CONFLICT`; DENY не создаёт ни записи идемпотентности, ни
эффекта.

## Цепочка, которая доказывается

```text
Identity (IS-001)
      ↓  эффективный тенант — только из IS-001
Authorization Boundary (IS-003)
      ↓  ALLOW — данные, а не доступ
Learning Boundary (этот компонент)
   ├─ запись существует у этого владельца?       иначе DENY
   ├─ единственный владелец — записан?           иначе DENY
   ├─ решение власти получено и авторитетно?     иначе DENY (закрыто)
   ├─ решение власти — ALLOW + permitted?        иначе DENY
   ├─ состояние и структура допустимы?           иначе INVALID_STATE_TRANSITION
   ├─ Idempotency-Key обязателен и binding тот же? иначе IDEMPOTENCY_KEY_REQUIRED /
   │                                              IDEMPOTENCY_CONFLICT
   └─ IS-005 исполняет эффект ровно один раз
      ↓
эффект записывается один раз; факт review и статус иерархии неизменяемы
```

Порядок шагов опубликован (`api.enforcement_model.enforcement_chain`) и
является частью поведения: каждый шаг может только отказать. Операция над
данными вызывается ровно один раз и только после полного прохождения цепочки.
Вопрос о команде создания Course называет тенант из IS-001 — цель ещё не
существует; вопросы об остальных сущностях называют родителя и его тенант из
собственного хранилища — никогда из заявления вызывающей стороны.

## Модель данных

### Submission (срезы 1–3)

| Поле | Смысл |
|---|---|
| `submission_id` | идентификатор работы (opaque) |
| `assignment_id` | задание-родитель |
| `student_identity_id` | opaque-ссылка на идентичность; профиль вне Learning |
| `attempt` | номер попытки (≥ 1) |
| `content` | тело работы (object) |
| `status` | `DRAFT → SUBMITTED` — ровно эти два состояния |
| `created_at` / `updated_at` | метки времени |
| `reviewed_by` | opaque-ссылка на verified identity учителя; `null` до review; неизменяем после первого review |
| `reviewed_at` | timestamp успешного review; `null` до review; неизменяем после первого review |

Работа принадлежит тому же тенанту, что и её задание — это инвариант
хранилища, а не заявление вызывающей стороны. Ответ списка содержит только
работы запрошенного задания в детерминированном порядке `submission_id`;
пустое задание отвечает `{"items": []}`. Пагинации, сортировки, поиска и
частичных наборов нет. Оценок, комментариев,
review-состояний, прогресса, Enrollment, сущностей Review/Evaluation/Grade/
Feedback/LearningResult в компоненте нет: review — это ровно два nullable поля.

### Content hierarchy (срез 4)

| Сущность | Поля | Lifecycle |
|---|---|---|
| `Course` | `course_id`, `tenant_id`, `title`, `description`, `status`, `created_by`, `created_at`, `updated_at` | `DRAFT → PUBLISHED → ARCHIVED`; без `UNPUBLISH` и `UNARCHIVE` |
| `Module` | `module_id`, `course_id`, `tenant_id`, `title`, `position`, `status` | создаётся только `DRAFT`; состояние меняет только публикация/архив Course |
| `Lesson` | `lesson_id`, `module_id`, `tenant_id`, `title`, `content`, `position`, `status` | создаётся только `DRAFT`; состояние меняет только публикация/архив Course |
| `Assignment` | существующая модель + `lesson_id`, `title`, `instructions` | новые — только `DRAFT`; поток Submission не меняется |

`tenant_id` и владелец — факты хранилища каждой записи, а не заявления
вызывающей стороны. Ответ чтения иерархии (`GET /courses/{course_id}`) —
навигационная цепочка Course → Module → Lesson → Assignment в
детерминированном порядке (`position`, затем идентификатор). Тенант и
владелец в публикуемых представлениях не раскрываются.

## Публичный контракт

- HTTP: `GET /api/v1/learning/submissions/{submission_id}` (операция
  `learning.submissions.read`, срез 1),
  `GET /api/v1/learning/assignments/{assignment_id}/submissions` (операция
  `learning.submissions.list`, срез 2),
  `POST /api/v1/learning/submissions/{submission_id}/review` (операция
  `learning.submissions.review`, срез 3) и семь операций авторинга среза 4 —
  `POST /courses`, `GET /courses/{course_id}`,
  `POST /courses/{course_id}/modules`, `POST /modules/{module_id}/lessons`,
  `POST /lessons/{lesson_id}/assignments`, `POST /courses/{course_id}/publish`,
  `POST /courses/{course_id}/archive` — см. `contract/openapi.yaml`
  (servers: `/api/v1/learning`, относительные пути) и
  `contract/component_contract.json`;
- уровень 0 (in-process): `learning_service.reader.LearningClient` — те же
  десять операций, значения туда и обратно.

`Authorization` несёт credential субъекта: компонент сам не проверяет
никаких credentials. `X-Tenant-Id` — только cross-check (LAW-16a), он никогда
не выбирает эффективный тенант команды. `Idempotency-Key` обязателен для всех
шести команд изменения состояния и доставляется через IS-005.

## Отказы

Отказы используют утверждённый конверт SCS-001:

```json
{
  "error": {
    "code": "AUTHORIZATION_DENIED",
    "message": "Operation is not permitted.",
    "details": {"reason": "permission_not_granted"}
  },
  "request_id": "opaque-id",
  "correlation_id": "opaque-id"
}
```

| Статус | Код | Причины (`error.details.reason`) |
|---|---|---|
| `400` | `IDEMPOTENCY_KEY_REQUIRED` | `idempotency_key_required` — команда без ключа |
| `401` | `AUTHENTICATION_REQUIRED` | `missing_identity`, `invalid_identity`, `unknown_identity` |
| `403` | `AUTHORIZATION_DENIED` | опубликованные `DENY`-причины IS-003 без изменений; `owner_mismatch` |
| `404` | `NOT_FOUND` | `assignment_unknown`, `submission_unknown`, `course_unknown`, `module_unknown`, `lesson_unknown` — решение не запрашивается |
| `409` | `INVALID_STATE_TRANSITION` | `invalid_state_transition` — review только для `SUBMITTED`; создание только под `DRAFT`-родителя; publish только для `DRAFT`; archive только для `PUBLISHED` |
| `409` | `ALREADY_REVIEWED` | `already_reviewed` — факт review неизменяем; другой ключ не перезаписывает первый review |
| `409` | `IDEMPOTENCY_CONFLICT` | `idempotency_conflict` — ключ уже использован с другим binding |
| `422` | `VALIDATION_ERROR` | `validation_error` — payload не проходит правила команды (пустой/длинный title, отрицательный position) |
| `503` | `DEPENDENCY_UNAVAILABLE` | `authorization_unavailable` — зависимость не ответила или вне контракта |

Статус выбирает код; стабильная внутренняя причина сохраняется в
`error.details.reason`; `request_id` / `correlation_id` совпадают с записью
аудита. Других конвертов отказа нет. Запрос, который не читается опубликованной
схемой (неизвестное поле, пропущенное поле, неверный тип), отказывается до
любого обработчика с `422 INVALID_REQUEST` и причиной `malformed_request`.

## Зависимости

| Компонент | Механизм | Операции |
|---|---|---|
| IS-001 `identity` (`>=0.3.0,<0.4.0`) | клиент над опубликованным API | `resolve_context` (только команда create-course) |
| IS-003 `authorization` (`>=0.1.0,<0.2.0`) | клиент над опубликованным API | `decide` |
| IS-005 `idempotency_guard` (`>=0.1.0,<0.2.0`) | внутренняя consumer surface | `execute` (все команды изменения состояния) |

**Ни один модуль `learning_service` не импортирует `authorization_service`,
`identity_service` или `tenant_authority`.** IS-005 — единственная прямая
зависимость-библиотека (guard), как и в IS-006: компонент не создаёт второй
механизм идемпотентности. IS-001 потребляется командой create-course, у
которой ещё нет цели: тенант вопроса авторизации может быть только
эффективным тенантом verified identity. Порт, значения и адаптер устроены как
в IS-004: вопрос о ресурсе формулируется с `tenant_id` из собственного
хранилища владельца.

## Данные компонента

| Набор | Область | Комментарий |
|---|---|---|
| `courses` | `tenant-scoped` | ровно один владелец; lifecycle `DRAFT → PUBLISHED → ARCHIVED` |
| `modules` | `tenant-scoped` | ровно один Course того же тенанта; регистрация только в `DRAFT` |
| `lessons` | `tenant-scoped` | ровно один Module того же тенанта; регистрация только в `DRAFT` |
| `assignments` | `tenant-scoped` | ровно один владелец; регистрация явная; срез 4 добавляет `lesson_id`/`title`/`instructions` |
| `submissions` | `tenant-scoped` | родитель и тенант проверяются при регистрации; review fact — два nullable поля |
| `access_audit` | `platform-scoped` | append-only журнал каждой попытки доступа |

## Тесты

| Файл | Что доказывает |
|---|---|
| `tests/test_learning_slice1.py` | чтение работы: успех, изоляция, отказы, аудит (prerequisite) |
| `tests/test_learning_slice2.py` | teacher discovery: успех, фильтрация, пустой список, изоляция, отказы, аудит |
| `tests/test_learning_slice3.py` | review: успех, replay, conflict, состояние, изоляция, отказы, аудит |
| `tests/test_learning_authoring.py` | авторинг: создание иерархии, сироты, cross-tenant, валидация, чтение навигации |
| `tests/test_learning_authoring_lifecycle.py` | публикация/архив: атомарность, идемпотентность, неизменяемость, чтения student |
| `tests/test_learning_authoring_security.py` | цепочка для команд авторинга: identity, гранты, изоляция, зависимости |
| `tests/test_learning_contract.py` | conformance контракта к живой реализации |
| `tests/test_learning_boundary.py` | отсутствие обходов и второго механизма авторизации/тенанта/идемпотентности |
