# Saga / Workflow Consistency Boundary (IS-006)

**Класс:** B — Platform Service
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `saga` (логическая схема `saga` — только состояние workflow)
**Задача:** Issue #18
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §6.3, §6.4, §6.5, §26, §27,
LAW-04, LAW-16, LAW-16a

IS-006 доказывает минимальную границу саги, требуемую архитектурой:
**многошаговая бизнес-операция имеет явное, наблюдаемое и ограниченное
состояние workflow и всегда приходит в определённое терминальное состояние** —
`COMPLETED`, `COMPENSATED` или `FAILED` — после успеха, retry, компенсации или
отказа.

Это не workflow-платформа. Компонент не содержит брокера, пула воркеров,
планировщика, внешнего движка, распределённого координатора, распределённых
блокировок и event bus: один ограниченный in-memory store состояния workflow и
один детерминированный in-process executor.

## Что именно доказывается

```text
IS-001 Identity
   ↓  эффективный Tenant — только из проверенной идентичности (LAW-16a)
Saga (этот компонент) — владеет только состоянием workflow
   ↓  каждый шаг и каждая компенсация — через IS-005
IS-005 Idempotency / Command Safety Boundary
   ↓  ровно один бизнес-эффект на шаг; replay вместо повторного применения
Component contract бизнес-компонента
   ↓  решение IS-003 + контроль владельца данных
Component data owner — бизнес-эффект происходит только здесь
```

Сага не может обойти границу компонента или владельца данных: у неё нет ни
одного handle на чужой store, engine, сессию или внутренний тип, и она не
хранит копий бизнес-данных. Путь всегда
`Saga → component contract → component data owner`.

## Модель состояния

Состояния и переходы — закрытые карты (`saga.models.SAGA_TRANSITIONS`,
`STEP_TRANSITIONS`). Переход, которого нет в карте, отклоняется store и ничего
не записывает, поэтому неопределённое частичное состояние невозможно зафиксировать
даже по ошибке.

```text
Сага:  STARTED → RUNNING → COMPLETED
                     ↓
                COMPENSATING → COMPENSATED
                     ↓
                   FAILED            RUNNING → FAILED (если применять было нечего)

Шаг:   PENDING → RUNNING → COMPLETED → COMPENSATING → COMPENSATED
                     ↓                       ↓
                 RETRYING → RUNNING     COMPENSATION_FAILED
                     ↓
                   FAILED
```

`RETRYING` — состояние шага, а не отдельное глобальное состояние саги.
Терминальная сага не может содержать шаг в `RUNNING`, `RETRYING` или
`COMPENSATING`: store отказывается записать такое состояние.

Контракт саги из §6.4 реализован полностью: `saga_id`, `correlation_id`,
`tenant_id`, `step_id`, `state`, `retry_policy`, `timeout`, `compensation`,
`idempotency`, `terminal_state`. Компенсация объявляется вместе с шагом, то
есть до его выполнения; шаг, которому нечего отменять, объявляет явный no-op.

## Восстановление

1. Шаг заявил retryable-отказ → повтор в пределах объявленного `RetryPolicy`
   (без задержек по умолчанию; расписание детерминировано и проверяемо).
2. Отказ не retryable или попытки исчерпаны → шаг `FAILED`, сага переходит в
   `COMPENSATING` и отменяет выполненные шаги в обратном порядке объявления.
3. Все компенсации успешны → `COMPENSATED`; хотя бы одна не удалась →
   терминальный `FAILED` с сохранённой информацией об отказе.
4. Применённых шагов не было → `RUNNING → FAILED` напрямую.

Timeout не является молчаливым успехом: попытка, вернувшаяся позже объявленного
`timeout_seconds`, фиксируется как отказ с путём восстановления. В Level 0
зависший обработчик нельзя прервать (нет воркера, которого можно отменить),
поэтому timeout проверяется в момент возврата попытки — это ограничение
заявлено, а не скрыто.

## Идемпотентность

Второго механизма идемпотентности здесь нет. Команда шага delivers через
существующую границу IS-005 под стабильной идентичностью
`saga:<saga_id>:step:<step_id>:<phase>`, не зависящей от номера попытки:

* повторная доставка шага → replay, бизнес-эффект не применяется второй раз;
* для шага, уже записанного как выполненный, эффект вообще не передаётся в
  guard — запрашивается replay, а расхождение фиксируется как явная ошибка;
* компенсация имеет собственную идентичность команды и поэтому сама
  идемпотентна;
* тот же шаг с другим payload или от другой идентичности → конфликт IS-005,
  который фиксируется как security-sensitive отказ шага.

## Наблюдаемость

Каждый переход саги и шага пишется в append-only журнал с контекстом §26:
`saga_id`, `step_id`, `state`, `reason`, `tenant_id`, `identity_id`,
`actor_service_id`, `platform_id`, `request_id`, `correlation_id`, `trace_id`,
`timestamp`, `component_id`, `component_version`, `security_sensitive`.
Отказы по безопасности (tenant mismatch, отсутствие разрешения, чужая
идентичность, конфликт идемпотентности) и терминальные переходы наблюдаемы.
События replay/conflict границы IS-005 пишутся в тот же журнал.
Учётные данные не сохраняются: credential предъявляется на каждую доставку,
в журнале остаётся идентичность.

## Инварианты

`S-001`…`S-012` объявлены в `contract/component_contract.json` и доказаны
тестами: стабильный `saga_id`; явное состояние саги и шага; безопасность
повторной доставки через IS-005; ошибки не теряются; определённый путь
восстановления; идемпотентная компенсация через существующие границы;
гарантированное терминальное состояние; неизменяемый tenant-контекст (IS-001 —
источник, IS-002 — авторитет жизненного цикла); отсутствие второго механизма
identity/tenant/authz/idempotency; аудируемость переходов; невозможность обойти
границу компонента или владельца данных.

## Что этим не является

Kafka, RabbitMQ, Redis, Celery, Temporal и другие внешние движки;
распределённый координатор транзакций; распределённые блокировки; production
distributed recovery; event bus; декомпозиция на микросервисы; новые
OIDC/IAM, tenant-context, authorization или idempotency механизмы; Component
Catalog, Composer, Golden Bundles, Release Trains; универсальный DAL; прямой
доступ к БД чужого компонента. Сага требуется только для действительно
многошаговых операций, пересекающих независимые границы бизнеса/владельцев
данных, и не навязывается каждой локальной атомарной операции.

## Состав

```text
src/saga/models.py     — модель состояния, объявления шагов, контексты, fingerprint
src/saga/executor.py   — детерминированный in-process executor
src/saga/store.py      — ограниченный in-memory store состояния workflow + журнал
src/saga/ports.py      — порты: tenant context (IS-001), command safety (IS-005)
src/saga/consumed.py   — локальные типы того, что потребляется от IS-001
src/saga/errors.py     — словарь отказов компонента
components/saga/contract/component_contract.json
components/saga/contract/openapi.yaml
tests/test_saga_acceptance.py      — сценарии A–H
tests/test_saga_state_model.py     — модель состояния и терминальность
tests/test_saga_boundary.py        — границы и владение данными
tests/test_saga_observability.py   — аудит и корреляционный контекст
tests/test_saga_idempotency.py     — интеграция с IS-005
tests/test_saga_contract.py        — валидация машиночитаемого контракта
```

Публичная поверхность: `SagaExecutor` (`start`, `run`, `deliver_step`,
`snapshot`, `audit_trail`), `SagaDefinition`, `StepDefinition`, `RetryPolicy`,
`SagaState`, `StepState`, `SagaSnapshot`, `StepSnapshot`, `StepContext`,
`FailureInfo`, `SagaAuditEvent` и словарь ошибок. Модули `executor`, `store`,
`models`, `ports`, `consumed`, `errors` являются внутренними.
