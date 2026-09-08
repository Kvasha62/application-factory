# Identity / Tenant Context (IS-001)

**Класс:** B — Platform Service  
**Уровень:** Level 0 — Modular Monolith  
**Версия компонента:** 0.3.0  
**Владелец данных:** `identity` (логическая схема `identity`)

Минимальный platform service, доказывающий архитектурные инварианты
Identity, Tenant Context и data-owner authorization.

Это не IAM-продукт и не фабричная механика.

## Контракт

Машиночитаемый Component Contract:
`components/identity/contract/component_contract.json`

## Граница

Внешнее взаимодействие только через опубликованный API `/api/v1`.
Другие компоненты не получают прямой доступ к внутреннему store.
`tenant_id` от вызывающей стороны — только cross-check.

## Источник состояния Tenant (с 0.2.0)

Реестр Tenant и lifecycle принадлежат Tenant Authority (IS-002). Identity:

- остаётся единственным источником `effective_tenant_id` — tenant выводится из
  проверенной идентичности, а не из заявления вызывающей стороны;
- читает состояние Tenant и принадлежность Platform Instance через опубликованный
  контракт Tenant Authority (`tenant_authority.reader.TenantAuthorityClient`:
  `lookup`, `lifecycle_decision`) и проверяет их на своей границе авторизации;
- получает от композиционного корня только value-only клиент над контрактом:
  engine, хранилище, журнал аудита, операции мутаций и ASGI-приложение Tenant
  Authority identity не видит и не может вызвать — в состоянии клиента лежат три
  значения (дескриптор канала, credential, привязка Platform Instance), а не
  транспортный объект, так что и обход замыканий ни к чему не ведёт;
- публикует из `identity_service.api` только собственный API: `create_app(engine)`
  и маршруты identity (набор имён зафиксирован в `__all__` модуля). Сборка Level 0 (deployment Tenant Authority, клиент и оба
  HTTP-приложения) выполняется композиционным корнем либо тест-фикстурой —
  модуль API identity ничего не собирает и чужое приложение не отдаёт;
- не хранит копии состояний Tenant и не определяет второй tenant-context
  механизм; локальный набор значений `TenantStatus` удалён.

Переход `active → suspended` в Tenant Authority блокирует обычные операции
identity сразу, без дополнительных механизмов синхронизации: состояние не
кэшируется, а перечитывается на границе авторизации.

Churn Tenant-состояния над границей: чужой Platform Instance отвечает
`tenant_not_found` (причина `foreign_tenant` остаётся в журнале Tenant Authority),
и identity отображает это как `TENANT_UNKNOWN`.

## Контекст для границы авторизации (с 0.3.0)

Authorization Boundary (IS-003, Issue #12) не имеет права выводить tenant
самостоятельно: `effective_tenant_id` берётся только из проверенной
идентичности. Чтобы это было выполнимо, identity публикует один аддитивный
read — `GET /api/v1/context`:

- отвечает на два вопроса сразу: кто субъект (`identity_id`, `kind`, `subject`)
  и какой Tenant для него действует (`tenant_id`, `platform_id`,
  `source = verified_identity`);
- `X-Tenant-Id` остаётся исключительно cross-check: несовпадение — `403`
  `tenant_mismatch`, а не выбор другого Tenant;
- ничего не разрешает: состояние Tenant в ответ не входит (его владелец —
  IS-002), permissions в ответ не входят (их владелец — IS-003);
- аудируется как `identity.context` с `request_id` и `correlation_id`
  вызывающей стороны — и при выдаче контекста, и при отказе;
- потребителю выдаётся value-only клиент
  `identity_service.reader.IdentityContextClient` с единственной операцией
  `resolve_context`.

Изменение аддитивное (MINOR, §7 ARCHITECTURE.md): существующие операции
`/api/v1/me`, `/api/v1/records/{id}` и их поведение не менялись.

## Запуск тестов

```text
python -m pip install -e ".[dev]"
python -m pytest
```
