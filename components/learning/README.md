# SCS-001 Learning — Teacher Submission Review (Slice 3)

**Класс:** A — Business System / SCS
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `learning` (логическая схема `learning`)
**Задача:** SCS-001 Slice 3 — Issue #24 (на prerequisite среза 1)
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §5.3, §5.4, §6.1, §6.2,
§6.5, §26, §27, LAW-03, LAW-04, LAW-16, LAW-16a

SCS-001 — бизнес-компонент, владелец учебных данных. Срез 3 добавляет к
чтению отдельной работы из среза 1 ровно одну команду изменения состояния —
review работы учителем:

```text
Teacher → Record review of one Submission (this slice)
```

Это не grading-платформа, не analytics и не progress-трекинг: компонент
владеет ровно двумя наборами данных (задания и работы) и ровно одной
границей контроля.

## Семантика review

Review означает ровно одно: учитель фиксирует факт, что конкретная Submission
проверена. Review — это **не** оценка, не балл, не grading, не feedback, не
комментарий, не evaluation и не LearningResult. Review не меняет lifecycle
Submission и не создаёт сущности Review.

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
   ├─ Submission == SUBMITTED?                   иначе INVALID_STATE_TRANSITION
   ├─ Idempotency-Key обязателен и binding тот же? иначе IDEMPOTENCY_KEY_REQUIRED /
   │                                              IDEMPOTENCY_CONFLICT
   └─ IS-005 исполняет эффект ровно один раз
      ↓
reviewed_by / reviewed_at записываются; lifecycle остаётся SUBMITTED
```

Порядок шагов опубликован (`api.enforcement_model.enforcement_chain`) и
является частью поведения: каждый шаг может только отказать. Операция над
данными (`serve_submission` / `apply_review`) вызывается ровно один раз и
только после полного прохождения цепочки.

## Модель данных

| Поле | Смысл |
|---|---|
| `submission_id` | идентификатор работы (opaque) |
| `assignment_id` | задание-родитель |
| `student_identity_id` | opaque-ссылка на идентичность; профиль вне Learning |
| `attempt` | номер попытки (≥ 1) |
| `content` | тело работы (object) |
| `status` | `DRAFT → SUBMITTED` — ровно эти два состояния |
| `created_at` / `updated_at` | метки времени |
| `reviewed_by` | opaque-ссылка на verified identity учителя; `null` до review |
| `reviewed_at` | timestamp успешного review; `null` до review |

Работа принадлежит тому же тенанту, что и её задание — это инвариант
хранилища, а не заявление вызывающей стороны. Оценок, комментариев,
review-состояний, прогресса, Enrollment, сущностей Review/Evaluation/Grade/
Feedback/LearningResult в срезе нет: review — это ровно два nullable поля.

## Публичный контракт

- HTTP: `GET /api/v1/learning/submissions/{submission_id}` (операция
  `learning.submissions.read`, срез 1) и
  `POST /api/v1/learning/submissions/{submission_id}/review` (операция
  `learning.submissions.review`, этот срез) — см. `contract/openapi.yaml`
  (servers: `/api/v1/learning`, относительные пути) и
  `contract/component_contract.json`;
- уровень 0 (in-process): `learning_service.reader.LearningClient` — две
  операции, значения туда и обратно.

`Authorization` несёт credential субъекта: компонент сам не проверяет
никаких credentials. `X-Tenant-Id` — только cross-check (LAW-16a).
`Idempotency-Key` обязателен для review и доставляется через IS-005.

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
| `404` | `NOT_FOUND` | `submission_unknown` — решение не запрашивается |
| `409` | `INVALID_STATE_TRANSITION` | `invalid_state_transition` — review только для `SUBMITTED` |
| `409` | `IDEMPOTENCY_CONFLICT` | `idempotency_conflict` — ключ уже использован с другим binding |
| `503` | `DEPENDENCY_UNAVAILABLE` | `authorization_unavailable` — зависимость не ответила или вне контракта |

Статус выбирает код; стабильная внутренняя причина сохраняется в
`error.details.reason`; `request_id` / `correlation_id` совпадают с записью
аудита. Других конвертов отказа нет.

## Зависимости

| Компонент | Механизм | Операции |
|---|---|---|
| IS-003 `authorization` (`>=0.1.0,<0.2.0`) | клиент над опубликованным API | `decide` |
| IS-005 `idempotency_guard` (`>=0.1.0,<0.2.0`) | внутренняя consumer surface | `execute` (только команда review) |

**Ни один модуль `learning_service` не импортирует `authorization_service`,
`identity_service` или `tenant_authority`.** IS-005 — единственная прямая
зависимость-библиотека (guard), как и в IS-006: компонент не создаёт второй
механизм идемпотентности. Порт, значения и адаптер устроены как в IS-004:
вопрос о ресурсе формулируется с `tenant_id` из собственного хранилища
владельца.

## Данные компонента

| Набор | Область | Комментарий |
|---|---|---|
| `assignments` | `tenant-scoped` | ровно один владелец; регистрация явная |
| `submissions` | `tenant-scoped` | родитель и тенант проверяются при регистрации; review fact — два nullable поля |
| `access_audit` | `platform-scoped` | append-only журнал каждой попытки доступа |

## Тесты

| Файл | Что доказывает |
|---|---|
| `tests/test_learning_slice1.py` | чтение работы: успех, изоляция, отказы, аудит (prerequisite) |
| `tests/test_learning_slice3.py` | review: успех, replay, conflict, состояние, изоляция, отказы, аудит |
| `tests/test_learning_contract.py` | conformance контракта к живой реализации |
| `tests/test_learning_boundary.py` | отсутствие обходов и второго механизма авторизации/тенанта/идемпотентности |
