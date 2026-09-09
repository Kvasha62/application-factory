# Idempotency / Command Safety Boundary (IS-005)

**Класс:** A — Platform Service (внутренняя consumer surface, не HTTP)
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `idempotency` (логическая схема `idempotency` — in-memory, bounded)
**Задача:** Issue #16
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §6.5, §26, §27,
LAW-04, LAW-07, LAW-16, LAW-16a

IS-005 доказывает минимальную границу безопасности команд, требуемую
архитектурой: **логическая команда исполняется ровно один раз и сохраняет
результат** (I-001); точный повтор возвращает сохранённый результат и **не
исполняет эффект повторно** (I-002); повтор тем же ключом с другим контекстом
— конфликт (I-003…I-005); отказ авторизации или сбой зависимости **не создают
запись** (I-006).

Это не сервис идемпотентности с собственным HTTP API, очередью или БД.
Компонент — один ограниченный in-memory guard, вызываемый бизнес-сервисами
внутри процесса после решения авторизации; он не является ни HTTP-приложением,
ни очередью, ни вторым механизмом identity/tenant (I-007).

## Что именно доказывается

```text
Бизнес-команда (learning / saga / …)
   ↓  решение IS-003 уже принято; эффект авторизован владельцем данных
IdempotencyGuard.execute (эта граница)
   ↓  ровно один эффект на логическую команду; replay — без повторного эффекта
Бизнес-эффект происходит один раз
```

Защита от повторного применения выполняется **до** исполнения эффекта;
компонент не меняет ни бизнес-логику, ни решение авторизации, ни tenant-контекст.

## Публикуемая поверхность

Единственная consumer surface — модуль `idempotency.guard`, тип
`IdempotencyGuard`, операция `execute` (контракт `components/idempotency/contract/`).
HTTP-контракта нет (`api.openapi: not_applicable`): OpenAPI-документ фиксирует
только value-схемы заголовка `Idempotency-Key` и ответа конфликта (409).

```python
guard.execute(
    key,                       # Idempotency-Key (обязателен; пустой ключ — отказ)
    *,
    identity, tenant_id,       # фактические (не заявленные) identity/tenant из IS-001
    operation, resource,
    fingerprint,               # отпечаток payload: различие payload при том же ключе = конфликт
    request_id=None,
    correlation_id=None,
    effect,                    # callable без аргументов: бизнес-эффект
)
```

## Повтор и конфликт

* **Точный replay** — тот же ключ и тот же полный binding
  (identity + tenant + operation + resource + fingerprint): возвращается
  сохранённый результат, эффект не исполняется; событие `idempotency_replay`
  уходит в audit-sink обёртки.
* **Конфликт** — тот же ключ, но другой binding (I-003…I-005): эффект не
  исполняется, поднимается `IdempotencyConflict`; событие
  `idempotency_conflict` — security-sensitive (I-008) и аудитится.
* **Первое исполнение** — эффект вызывается один раз; его результат
  сохраняется под ключом. Если эффект выбрасывает исключение (включая отказ
  авторизации на границе владельца данных), запись **не создаётся** (I-006) —
  повторный запрос с тем же ключом снова попытается исполнить команду, а не
  вернёт «успешный» результат неудавшейся.

Важно: replay ≠ retry. Replay — повтор **той же** логической команды (тот же
binding): эффект один раз. Retry — повтор **неудавшейся** попытки: поскольку
сбой не записывает запись, retry проходит как первичное исполнение.

## Конкуренция

* Глобальная блокировка защищает только создание per-key блокировок.
* Каждая команда (ключ) исполняется под своей `threading.Lock`: параллельные
  запросы с одним ключом сериализуются, и ровно один из них исполняет эффект.
* Store — bounded in-memory: записи живут внутри процесса; при перезапуске
  состояние не восстанавливается (Level 0, допустимо контрактом; персистентная
  идемпотентность — задача соответствующего уровня зрелости).

## Наблюдаемость

Guard не генерирует собственный observability-контекст: он получает
`request_id`/`correlation_id` и передаёт их в audit-sink обёртки вместе с
`identity`, `tenant_id`, `operation`. Аудит — append-only журнал обёртки
(бизнес-сервиса), который никогда не записывает креденшелы.

## Инварианты

| Инвариант | Содержание |
|---|---|
| I-001 | Одна логическая команда исполняется ровно один раз и сохраняет результат |
| I-002 | Точный replay возвращает тот же результат без повторного эффекта |
| I-003 | Тот же ключ с другим fingerprint/payload — CONFLICT |
| I-004 | Тот же ключ от другой identity — CONFLICT |
| I-005 | Тот же ключ в другом tenant-контексте — CONFLICT |
| I-006 | DENY или сбой зависимости не создают запись и не исполняют эффект |
| I-007 | Guard не создаёт второй механизм identity или tenant |
| I-008 | Cross-tenant / cross-identity replay — security-sensitive и аудитятся |

## Что этим не является

* Не HTTP-сервис и не отдельный процесс: consumer surface — модуль, вызываемый
  в процессе бизнес-сервисом.
* Не очередь, брокер, внешний кэш или персистентное хранилище.
* Не механизм аутентификации/авторизации: guard исполняется **после** решения
  IS-003 и делегирует ему безопасность (`authn: delegated`,
  `authz.enforcement_boundary: none`).
* Не механизм tenant-контекста: identity/tenant передаются guard'ом из
  проверенного контекста IS-001 обёрткой, а не выводятся им.
* Не универсальный deduplication-движок: binding привязан к конкретной
  операции и ресурсу конкретного бизнес-компонента.

## Состав

```text
src/idempotency/
├── __init__.py    # публикуемая поверхность: IdempotencyGuard, IdempotencyConflict, IdempotencyRecord
├── guard.py       # IdempotencyGuard: per-key locks, replay/conflict, exactly-once effect
├── models.py      # IdempotencyRecord (frozen value)
└── errors.py      # IdempotencyConflict
```

Контракт: `components/idempotency/contract/component_contract.json`
(machine-readable, 10 обязательных полей) + `openapi.yaml` (value-схемы).

## Тесты

* `tests/test_idempotency_guard.py` — 11 тестов: exactly-once, replay без
  повторного эффекта, конфликты по identity/tenant/fingerprint, «сбой не
  создаёт запись», параллельные запросы с одним ключом.
* `tests/test_saga_idempotency.py` — 10 тестов: command identity шага
  `saga:<saga_id>:step:<step_id>:<phase>`, replay/конфликт на уровнях
  доставки и компенсации, retry ≠ replay.
* `tests/test_learning_authoring_lifecycle.py`,
  `tests/test_learning_authoring_concurrency.py` — Idempotency-Key
  на HTTP-командах SCS-001: replay, binding conflict, race двух команд.
* `tests/test_learning_contract.py`, `tests/test_saga_contract.py` —
  контракт consumer surface соответствует реализации.
