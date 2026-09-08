# Tenant Authority / Tenant Lifecycle (IS-002)

**Класс:** B — Platform Service
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `tenant_authority` (логическая схема `tenant_authority`)
**Задача:** Issue #10
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §2.3, §5.4, LAW-16a,
ADR-0010 AMD-01/AMD-02/AMD-03/AMD-10/AMD-11

Tenant Authority — единственный авторитетный механизм управления Tenant и его
lifecycle внутри Platform Instance.

Это не IAM, не provisioning-инфраструктура и не фабричная механика.

## Цепочка, которая доказывается

```text
Verified Identity
      ↓
Identity / Tenant Context        (IS-001 — источник effective_tenant_id)
      ↓
Tenant Authority                 (этот компонент)
   ├─ tenant exists?
   ├─ belongs to Platform Instance?
   ├─ lifecycle state?
   └─ operation permitted?
      ↓
Authorization Boundary
      ↓
Tenant-owned Resource
```

## Модель данных

Tenant Registry (область `platform-scoped`, ARCHITECTURE.md §5.4 / LAW-16a):

| Поле | Свойства |
|---|---|
| `tenant_id` | иммутабелен, непрозрачен, не переиспользуется (§11) |
| `platform_id` | ровно одна Platform Instance; неизменен после создания |
| `state` | ровно одно текущее состояние |
| `created_at` | неизменен |
| `updated_at` | меняется только при принятом переходе |

`tenant_registry`, `lifecycle_transitions`, `service_access`, `audit_events`,
`idempotency_keys` — внутренние данные компонента. Другой компонент не получает
к ним прямого доступа (T-009): ни к хранилищу, ни к журналу аудита, ни к
операциям записи.

## Lifecycle

```text
provisioning → active → suspended → deletion_requested → deleted
```

Разрешённые переходы (ровно те, что требуются архитектурой и задачей):

```text
provisioning → active
active       → suspended
suspended    → active
active       → deletion_requested
suspended    → deletion_requested
deletion_requested → deleted
```

Иных разрешённых переходов нет; недопустимый переход отклоняется с причиной
`invalid_transition` и попадает в аудит. `deleted` — терминальное состояние:
resurrection невозможен (T-006).

Operational policy минимальна и не содержит выдуманных правил: обычные
tenant-scoped операции запрещены в состояниях `provisioning`, `suspended`,
`deletion_requested`, `deleted`; разрешены в `active`.

## Публичный контракт

- HTTP: `/api/v1` — см. `contract/openapi.yaml` и `contract/component_contract.json`;
- уровень 0 (in-process): `tenant_authority.reader.TenantAuthorityClient` —
  ровно две операции чтения, `lookup` и `lifecycle_decision`.

Граница компонента контрактно-опосредованная, а не соглашенческая: клиент
выполняет опубликованный HTTP-контракт внутри процесса через
`tenant_authority.transport` — `open_channel` регистрирует приложение компонента
и отдаёт непрозрачный дескриптор канала, `call_contract` выполняет по нему
запрос; само ASGI-приложение потребителю не передаётся. Наружу отдаются только
значения из `tenant_authority.contracts`. Через опубликованную поверхность
недоступны хранилище, журнал аудита, таблица идемпотентности, операции мутаций,
engine и контрактное ASGI-приложение — ни под открытым именем, ни под закрытым
(`_source`, `_store`, `_engine`), ни через замыкания: состояние клиента — три
значения (`_channel`, `_credential`, `_expected_platform_id`), callables среди
них нет, поэтому обход `__closure__` / `cell_contents` упирается в строки.
Каждое чтение проходит аутентификацию сервисной идентичности, проверку права,
валидацию принадлежности Platform Instance и запись в аудит — тот же код, что и
для HTTP-запроса. Дескриптор канала отзывается, когда потребитель выпускает
клиента; неизвестный или отозванный дескриптор означает отказ, а не разрешение.

Отказ, выданный на чужой Tenant, приходит потребителю как `tenant_not_found`
(см. `api.denial_semantics`): точная причина `foreign_tenant` остаётся в журнале
аудита компонента. Сбой транспорта (не-JSON, отсутствие ответа) поднимает
`tenant_authority.errors.ContractViolation` — потребитель падает закрытым,
а не считает Tenant обслуживаемым.

Чтение состояния Tenant **не является** решением авторизации: компонент-владелец
данных сам применяет аутентификацию, права и tenant-границу на своей границе
(§6.2 ARCHITECTURE.md). Именно так это использует IS-001.

IS-001 обращается к авторитету дважды за запрос: один раз при выводе контекста
(`lookup` — существование Tenant и принадлежность Platform Instance), второй раз
при авторизации (`lifecycle_decision`). Это намеренно: снимок состояния, взятый
в начале запроса, не является основанием для решения, состояние перечитывается на
границе авторизации (см. `test_enforcement_is_live_not_a_snapshot_taken_at_context_resolution`).

Никаких событий, CDC-потоков и UI компонент не объявляет: их реализация в
IS-002 отсутствует, и в контракте они честно помечены как `declared_only`.

## Интеграция с IS-001

IS-001 остаётся единственным источником `effective_tenant_id`: вывод tenant из
проверенной идентичности не дублируется. Tenant Authority предоставляет
authoritative lookup состояния и принадлежности Tenant. Локальная копия
реестра Tenant в identity удалена, как и второй набор значений состояний —
второго механизма не появляется (T-004).

Композиция Level 0 выполняется в `tenant_authority.deployment.build_deployment()`;
объект `TenantAuthorityDeployment` (engine, store, config, HTTP-приложение) —
**внутренний** для сборки и наружу потребителю не передаётся: композиционный корень
вызывает `deployment.publish(credential=...)` и отдаёт потребителю только клиент.
Отдельного «runtime API» для потребителей больше нет.

Контрактное приложение компонента остаётся в руках композиционного корня: ни один
потребитель не делает его частью своей публичной поверхности (ни `identity_service.api`,
ни иной компонент не экспортирует ASGI-приложение Tenant Authority) — иначе чтение
через контракт превратилось бы в общий доступ к внутренностям компонента.

## Отказ в обслуживании по состоянию

| Состояние | Обычная tenant-scoped операция | Причина |
|---|---|---|
| `provisioning` | DENY | `provisioning_not_served` |
| `active` | ALLOW | `permitted` |
| `suspended` | DENY | `tenant_suspended` |
| `deletion_requested` | DENY | `tenant_deletion_requested` |
| `deleted` | DENY | `tenant_deleted` |

Tenant другой Platform Instance недоступен из контекста этой Platform Instance:
ответ совпадает с «нет такого Tenant» (`tenant_not_found`), точная причина
`foreign_tenant` сохраняется только в аудите.

## Аудит и наблюдаемость

Каждый принятый переход записывается как `lifecycle_transition` и как запись
аудита минимум с: `tenant_id`, `previous_state`, `new_state`, `actor_id`
(service identity), `timestamp`, `request_id`, `correlation_id`. Отказы
(недопустимый переход, чужой Tenant, отсутствие прав, идемпотентный конфликт)
аудируются в том же журнале.

Observability context соответствует §26 ARCHITECTURE.md: `timestamp`,
`environment`, `platform_id`, `component_id`, `component_version`, `tenant_id`,
`request_id`, `trace_id`, `correlation_id`, `service_id`. `saga_id` отсутствует
сознательно: компонент не выполняет межкомпонентных саг.

## Идемпотентность

`POST /api/v1/tenants` и `POST /api/v1/tenants/{tenant_id}/transitions`
принимают `Idempotency-Key`. Ключ привязан к actor, platform, операции, tenant и
fingerprint запроса: повтор возвращает результат исходной операции и не создаёт
второго эффекта; иной запрос под тем же ключом отклоняется как
`idempotency_conflict`.

## Non-goals

В объём IS-002 не входят: Component Catalog/Registry, Composer, Golden Bundles,
release trains, billing, управление пользователями, organizations, полноценный
IAM, сложные RBAC/ABAC, tenant UI, provisioning-инфраструктура, Kubernetes,
распределённые БД, кросс-tenant аналитика и физическое удаление данных всех
компонентов. `deleted` здесь — состояние lifecycle Tenant Authority, а не
реализация data deletion engine.

## Запуск тестов

```text
python -m pip install -e ".[dev]"
python -m pytest
```

Тесты поведенческие: переходы state machine проверяются полным матричным
перебором состояний через публичные операции, а не проверкой исходного текста.
