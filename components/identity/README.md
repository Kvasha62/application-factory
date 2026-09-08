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
  контракт Tenant Authority (`tenant_authority.contracts.TenantAuthorityReader`:
  `lookup`, `lifecycle_decision`) и проверяет их на своей границе авторизации;
- не хранит копии состояний Tenant и не определяет второй tenant-context
  механизм; локальный набор значений `TenantStatus` удалён.

Переход `active → suspended` в Tenant Authority блокирует обычные операции
identity сразу, без дополнительных механизмов синхронизации.

## Запуск тестов

```text
python -m pip install -e ".[dev]"
python -m pytest
```
