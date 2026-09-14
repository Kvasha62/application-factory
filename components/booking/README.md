# SCS-003 Booking — Resource, Availability Window, Reservation (MVP)

**Класс:** A — Business System / SCS
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `booking` (логическая схема `booking`)
**Задача:** SCS-003 Booking MVP — Issue #57
**Архитектурная основа:** `docs/ARCHITECTURE.md` §5.3, §5.4, §6.1, §6.2,
§6.5, LAW-03, LAW-04, LAW-13, LAW-16, LAW-16a; ADR-0014

SCS-003 — третий независимый бизнес-компонент проекта после Learning и
Commerce, владелец данных бронирования. Компонент делает ровно одну
вещь: позволяет забронировать ограниченный ресурс на интервал времени
без двойного бронирования. Живая поверхность — семь операций:

```text
Subject → Create / Read one owned Resource
Subject → Declare / List the Availability Windows of a Resource
Subject → Create / Read / Cancel one Reservation
```

Это не календарь, не планировщик персонала, не система оплаты и не
generic CRUD: компонент владеет фактами бронирования (какой ресурс, в
каком разрешённом окне, кем и на какой интервал занят) и ровно одной
границей контроля. Цены, оплата, capacity > 1, повторяющиеся
бронирования, листы ожидания, уведомления, календарная синхронизация,
поиск и списки — вне границы (ADR-0014 §12).

## Граница Resource → Availability Window → Reservation

- **Resource** — что может быть забронировано в тенанте: `resource_id`,
  `tenant_id`, `name`, `status` (`ACTIVE` | `INACTIVE`). Domain-agnostic:
  без Commerce- и Learning-сущностей, без цены и без capacity. Только
  `ACTIVE`-ресурс принимает новые резервирования.
- **Availability Window** — явный разрешённый интервал ресурса
  `[start_at, end_at)`. Это **разрешение, а не занятость**: окно не
  расходуется резервированиями, окна могут пересекаться между собой,
  слотов нет.
- **Reservation** — занятость интервала одним booker'ом:
  `reservation_id`, `tenant_id`, `resource_id`, `booker_identity_id`,
  `start_at`, `end_at`, `status` (`ACTIVE` | `CANCELLED`), `created_at`,
  `cancelled_at`. Только `ACTIVE`-резервирования занимают время.

Booker — непрозрачный `booker_identity_id` verified identity субъекта.
В теле команды его нет по построению; отменить резервирование может
только booker — другой субъект того же тенанта получает
`booker_mismatch` даже при наличии grant.

## Время

| Правило | Поведение |
|---|---|
| Интервал | полуоткрытый `[start_at, end_at)`, `start_at < end_at` |
| Вход | ISO 8601 **с явным смещением** (`+02:00`, `Z`); naive timestamp — `422 VALIDATION_ERROR` до любого решения |
| Хранение и сравнение | UTC; `10:00+02:00` и `08:00Z` — один и тот же момент для конфликта, вложенности и идемпотентности |
| Ответ | UTC с явным `+00:00` |
| Смежность | `[a, b)` и `[b, c)` не пересекаются |
| Вложенность | резервирование лежит **целиком внутри одного** окна (границы включительно); интервал, «перекинутый» через два окна, не лежит ни в одном |

## Создание резервирования и отсутствие двойного бронирования

Резервирование создаётся только при одновременном выполнении пяти условий
(ADR-0014 §8), каждое с детерминированным отказом:

1. ресурс существует в тенанте — иначе `404 resource_not_found`;
2. ресурс `ACTIVE` — иначе `409 resource_inactive`;
3. интервал валиден (aware, `start_at < end_at`) — иначе `422 validation_error`;
4. интервал целиком внутри одного окна — иначе `409 outside_availability`
   (`availability_not_found`, если окон нет вовсе);
5. нет пересечения ни с одним `ACTIVE`-резервированием ресурса — иначе
   `409 reservation_conflict`.

Проверка и запись выполняются **атомарно** внутри эксклюзивной критической
секции ресурса (`BookingStore.resource_section`). При гонке двух команд с
разными `Idempotency-Key` ровно одна получает `201`, вторая — `409
reservation_conflict`; ни одно чередование не приводит к двум `ACTIVE`
пересекающимся резервированиям. Чтения в секцию не входят. Отказ `409`
не записывает результат IS-005: тот же ключ может повторить попытку
после освобождения интервала. Уровень 0 использует lock процесса;
персистентное хранилище обязано воспроизвести ту же атомарность
(row lock / serializable-транзакция) — контрактом является инвариант,
а не механизм.

## Жизненный цикл

```text
ACTIVE ──cancel──▶ CANCELLED
```

Единственный переход. Отмена сохраняет запись (исторический факт) и
освобождает интервал; повторная отмена — `409 invalid_state_transition`;
`CANCELLED → ACTIVE`, удаление и редактирование не существуют ни в HTTP,
ни в хранилище.

## Цепочка, которая доказывается

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       запись существует здесь и её единственный
                         владелец — этот компонент        иначе DENY
      ↓
authorization_decision   IS-003 решает через порт; исход по умолчанию —
                         DENY; зависимость, которая не ответила или
                         ответила вне контракта, закрывает доступ
      ↓
owned_data_operation     только теперь читаются или пишутся
                         собственные данные
```

ALLOW от IS-003 — не доступ к данным: операция выполняется только здесь,
в конце цепочки. Эффективный тенант выводится из verified identity через
IS-001; `X-Tenant-Id` — только cross-check (LAW-16a). Каждая команда
изменения состояния передаётся в IS-005 ровно один раз: повтор с тем же
`Idempotency-Key` возвращает записанный результат без второго эффекта,
повтор с другим binding отказывается; fingerprint считается по
UTC-нормализованному интервалу. Обслужённые доступы и каждый отказ —
включая доменные (`reservation_conflict`, `outside_availability`,
`resource_inactive`, `invalid_state_transition`, `booker_mismatch`) и
replay/conflict IS-005 — записываются в собственный append-only журнал
компонента с `request_id` и `correlation_id`.

## Публичный контракт

HTTP (база `/api/v1/booking`, см. `contract/openapi.yaml` и
`contract/component_contract.json`):

- `POST /resources` (команда `booking.resources.create`, `Idempotency-Key`
  обязателен) и `GET /resources/{resource_id}` (`booking.resources.read`);
- `POST /resources/{resource_id}/availability`
  (`booking.availability.create`, ключ обязателен) и
  `GET /resources/{resource_id}/availability` (`booking.availability.read`,
  окна в порядке `start_at`);
- `POST /reservations` (`booking.reservations.create`, ключ обязателен),
  `GET /reservations/{reservation_id}` (`booking.reservations.read`) и
  `POST /reservations/{reservation_id}/cancel`
  (`booking.reservations.cancel`, ключ обязателен, тела нет).

Уровень 0 (in-process): `booking_service.reader.BookingClient` — те же
семь операций, значения туда и обратно.

`Authorization` несёт credential субъекта: компонент сам не проверяет
никаких credentials. `X-Tenant-Id` — только cross-check (LAW-16a), он
никогда не выбирает эффективный тенант команды.

## Отказы

Отказы используют утверждённый конверт:

```json
{
  "error": {
    "code": "RESERVATION_CONFLICT",
    "message": "Requested interval conflicts with an active reservation.",
    "details": {"reason": "reservation_conflict"}
  },
  "request_id": "opaque-id",
  "correlation_id": "opaque-id"
}
```

| Статус | Код | Причины (`error.details.reason`) |
|---|---|---|
| `400` | `IDEMPOTENCY_KEY_REQUIRED` | `idempotency_key_required` — команда без ключа |
| `401` | `AUTHENTICATION_REQUIRED` | `missing_identity`, `invalid_identity`, `unknown_identity` |
| `403` | `AUTHORIZATION_DENIED` | опубликованные `DENY`-причины IS-003 без изменений; `owner_mismatch`; `booker_mismatch` — чужое резервирование |
| `404` | `NOT_FOUND` | `resource_not_found`, `reservation_not_found` — решение не запрашивается |
| `409` | `RESOURCE_INACTIVE` | `resource_inactive` |
| `409` | `OUTSIDE_AVAILABILITY` | `outside_availability`, `availability_not_found` |
| `409` | `RESERVATION_CONFLICT` | `reservation_conflict` — пересечение с `ACTIVE`-резервированием |
| `409` | `INVALID_STATE_TRANSITION` | `invalid_state_transition` — повторная отмена |
| `409` | `IDEMPOTENCY_CONFLICT` | `idempotency_conflict` — ключ уже использован с другим binding |
| `422` | `VALIDATION_ERROR` | `validation_error` — пустое имя, неизвестный статус, naive/нечитаемая метка времени, `start_at >= end_at` |
| `503` | `DEPENDENCY_UNAVAILABLE` | `authorization_unavailable` — зависимость не ответила или вне контракта |

Статус выбирает код; стабильная внутренняя причина сохраняется в
`error.details.reason`; `request_id` / `correlation_id` совпадают с
записью аудита. Других конвертов отказа нет. Запрос, который не читается
опубликованной схемой (неизвестное поле — например `booker_identity_id`
в теле, пропущенное поле, неверный тип), отказывается до любого
обработчика с `422 INVALID_REQUEST` и причиной `malformed_request`.

## Зависимости

| Компонент | Механизм | Операции |
|---|---|---|
| IS-001 `identity` (`>=0.3.0,<0.4.0`) | клиент над опубликованным API | `resolve_context` (одна команда без цели: create resource) |
| IS-003 `authorization` (`>=0.1.0,<0.2.0`) | клиент над опубликованным API | `decide` |
| IS-005 `idempotency` (`>=0.1.0,<0.2.0`) | внутренняя consumer surface | `execute` (все команды изменения состояния) |

**Ни один модуль `booking_service` не импортирует `authorization_service`,
`identity_service`, `tenant_authority`, `commerce_service` или
`learning_service`.** IS-005 — единственная прямая зависимость-библиотека
(guard): компонент не создаёт второй механизм идемпотентности. Booking не
зависит от Commerce и Learning и не изменяет их; интеграции (оплата
бронирования, бронирование учебных ресурсов) — отдельные будущие решения
через опубликованные контракты.

## Данные компонента

| Набор | Область | Комментарий |
|---|---|---|
| `resources` | `tenant-scoped` | ровно один владелец; `ACTIVE` / `INACTIVE`; тенант из verified identity |
| `availability_windows` | `tenant-scoped` | ровно один Resource того же тенанта; разрешение, не занятость; UTC |
| `reservations` | `tenant-scoped` | ровно один Resource того же тенанта; opaque booker; `ACTIVE → CANCELLED`; история сохраняется |
| `access_audit` | `platform-scoped` | append-only журнал каждой попытки доступа |

Демо-данные (`seed_demo`): `res_a1` (ACTIVE) и `res_a2` (INACTIVE) в
`ten_a`, `res_b1` в `ten_b`, у каждого окно 2026-10-01 08:00–18:00 UTC.

## Тесты

| Файл | Что доказывает |
|---|---|
| `tests/test_booking_domain.py` | интервалы и хранилище: aware→UTC (в т.ч. DST-смещения), отказ naive, `start_at < end_at`, полуоткрытость и смежность, вложенность в одно окно, только `ACTIVE` занимает время, освобождение после отмены, `ACTIVE → CANCELLED` без обратного хода и удаления, структурные правила регистрации |
| `tests/test_booking_skeleton.py` | каркас: сборка, health/ready, создание/чтение ресурса через цепочку, replay/conflict, изоляция тенанта, cross-check `X-Tenant-Id`, отказы, аудит, published client |
| `tests/test_booking_reservations.py` | окна и резервирования через цепочку: UTC-ответы, пять условий с их отказами, смежность, `booker_mismatch`, grant-отказы, изоляция тенанта, replay/conflict/binding, отказ без записи IS-005, отмена и повторная отмена, аудит каждого исхода |
| `tests/test_booking_concurrency.py` | гонки реальных потоков с разными ключами: ровно один победитель, `409 reservation_conflict` проигравшему, никогда двух `ACTIVE` пересекающихся; смежные оба побеждают; same-key — один эффект; отмена против повторного бронирования; эксклюзивность секции без sleeps |
| `tests/test_booking_contract.py` | conformance контракта к живой реализации: маршруты, коды, причины, конверт, схемы ответов |
| `tests/test_booking_boundary.py` | отсутствие недопустимых cross-component импортов (в т.ч. Commerce/Learning); клиент несёт только значения; словарь публичной поверхности |
