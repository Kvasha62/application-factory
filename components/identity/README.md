# Identity / Tenant Context (IS-001)

**Класс:** B — Platform Service  
**Уровень:** Level 0 — Modular Monolith  
**Версия компонента:** 0.1.0  
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

## Запуск тестов

```text
python -m pip install -e ".[dev]"
python -m pytest
```
