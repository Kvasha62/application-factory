# Identity / Tenant Context (IS-001)

**Класс:** B — Platform Service  
**Уровень:** Level 0 — Modular Monolith  
**Версия компонента:** 0.2.0  
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
  engine, хранилище, журнал аудита и операции мутаций Tenant Authority identity
  не видит и не может вызвать;
- не хранит копии состояний Tenant и не определяет второй tenant-context
  механизм; локальный набор значений `TenantStatus` удалён.

Переход `active → suspended` в Tenant Authority блокирует обычные операции
identity сразу, без дополнительных механизмов синхронизации: состояние не
кэшируется, а перечитывается на границе авторизации.

Churn Tenant-состояния над границей: чужой Platform Instance отвечает
`tenant_not_found` (причина `foreign_tenant` остаётся в журнале Tenant Authority),
и identity отображает это как `TENANT_UNKNOWN`.

## Запуск тестов

```text
python -m pip install -e ".[dev]"
python -m pytest
```
