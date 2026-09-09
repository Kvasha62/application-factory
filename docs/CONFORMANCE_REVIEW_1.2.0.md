# CONFORMANCE REVIEW — ARCHITECTURE.md v1.2.0

**Объект аудита:** `Kvasha62/application-factory`, commit `8445e2bb707df2b9a16ae1a76074d018c620fe43` (`main`)
**Дата аудита:** 2026-09-09
**Вид документа:** отчёт о фактическом соответствии (не нормативный документ, не новый архитектурный закон)
**Канонический закон:** `docs/ARCHITECTURE.md` v1.2.0 (RATIFIED, основание — ADR-0010)

## 0..Scope, источники и метод

Изучены (полностью): `docs/ARCHITECTURE.md`, `docs/adr/ADR-0010-architecture-gap-review.md`,
`docs/adr/ADR-0011-scs-001-learning-content-authoring-boundary.md`, `docs/adr/README.md`,
`docs/DOCUMENTATION_BASELINE.md`, `docs/OPERATING_MODEL.md`, `README.md`,
`scripts/quality-gate.ps1`, `pyproject.toml`, `.github/CODEOWNERS`.

Изучено (полностью, все 16 133 строки `src/`): все модули семи компонентов
(`authorization_service`, `identity_service`, `learning_service`, `records_service`,
`tenant_authority`, `saga`, `idempotency`), все 7 machine-readable Component Contracts
(`components/*/contract/component_contract.json`) и все 7 OpenAPI-документов.

Изучено (полностью, все 16 668 строк `tests/`): 59 тестовых файлов, 621 тест-функция.

Фактически выполнено:

| Команда | Результат |
|---|---|
| `python -m compileall -q src` | **PASS** |
| `python -m pytest tests/` | **PASS — 730 passed** (0 failed, 0 error) |
| `ruff check .` (ruff 0.16.6) | **FAIL — 97 errors** (19 fixable) |
| `black --check .` (black 25.9.0 и 26.5.1) | **FAIL — 67 of 131 files would be reformatted** (число одинаково для обеих версий) |
| `scripts/quality-gate.ps1` | **FAIL** (шаг Ruff; см. раздел H) |

Ограничение среды: аудит выполнялся на Python 3.11.2 (Quality Gate требует 3.13).
Код не использует синтаксис, недоступный в 3.11: полная тестовая зелёная на 3.11
не отменяет требования `requires-python >= 3.13`, но подтверждает отсутствие
3.13-специфичных конструкций в проверяемом контуре.

Классификация каждого состояния — строго один из пяти классов:
`CONFORMING`, `PARTIAL`, `NON-CONFORMING`, `UNPROVEN`, `DOCUMENTATION GAP`.
`UNPROVEN` нигде не переводится в `CONFORMING` без доказательств.

---

## A. Executive Summary

```text
Architecture:    CONFORMING — ни одно нарушение LAW-01…LAW-16/LAW-16a не доказано;
                 реализация находится в standalone/foundation mode, разрешённом §4.1
Implementation:  PARTIAL — 7 компонентов внутренне конформны и изолированы;
                 для статуса publishable (§30) отсутствуют migrations, реальный
                 deploy artifact и event-контракты (не заявлены к публикации);
                 одна контрактная версия зависимости устарела (saga → identity)
Documentation:   NON-CONFORMING (factual) — DOCUMENTATION_BASELINE, ADR-каталог,
                 README и один OpenAPI содержат утверждения, противоречащие
                 фактическому состоянию (раздел D)
Tests:           CONFORMING — 730 тестов покрывают все существенные законы для
                 реализованного контура: границы, tenant isolation, fail closed,
                 idempotency, saga, audit, observability (раздел 5)
Quality Gate:    NON-CONFORMING — gate на текущем main красный: ruff 97 + black 67;
                 ruff/black не объявлены зависимостями, CI отсутствует (раздел H)
ADR compliance:  PARTIAL — ADR-0010 RATIFIED и корректно обосновывает 1.2.0;
                 ADR-0011 (PROPOSED) при этом уже реализован в main (срез 4 SCS-001);
                 каталог ADR не обновлён
```

Ключевой итог: **архитектурный закон 1.2.0 нарушен не является ни по одному из
доказанных фактов**. Все семь компонентов реализуют заявленные инварианты:
ownership-границы, единый источник tenant-контекста, fail-closed при дефектах
зависимостей, exactly-once идемпотентность, саги с терминальными состояниями,
аудит всех отказов. Проблемы проекта — процессные и доковые: красный Quality Gate
без задекларированных tooling-зависимостей, устаревший DOCUMENTATION_BASELINE,
нератифицированный ADR-0011 под уже смерженной реализацией и одна устаревшая
версионная декларация в контракте saga.

---

## 1. LAW-01…LAW-16 (+ LAW-16a): детальная проверка

Формат: требование → где реализовано → тесты → контракт/документ → вывод.

### LAW-01 — Business Boundary

* **Требование:** границы определяются бизнес-возможностями, а не техническими классами.
* **Реализация:** 7 компонентов организованы вокруг бизнес-возможностей: Identity /
  Tenant Context (IS-001), Tenant Authority (IS-002), Authorization Boundary (IS-003),
  Data Ownership / Resource Boundary (IS-004), Learning SCS-001, Saga / Workflow
  Consistency Boundary (IS-006), Idempotency / Command Safety Boundary (IS-005).
  `src/records_service/store.py` явно называет свою модель «proof model IS-004,
  this slice is not a generic data platform»; `src/saga/executor.py` — «not a
  workflow platform»; `src/authorization_service/engine.py` — «this component
  decides; it never enforces».
* **Тесты:** `tests/test_learning_boundary.py`, `tests/test_saga_boundary.py`
  (`test_the_component_imports_no_business_component_at_runtime`),
  `tests/test_authorization_boundary.py`.
* **Документы:** `components/*/README.md` (каждый описывает бизнес-возможность и
  non-goals), ADR-0011 (бизнес-цепочка `Course → Module → Lesson → Assignment →
  Submission → Teacher Review`).
* **Вывод:** **CONFORMING.**

### LAW-02 — Component Ownership

* **Требование:** каждый Component имеет одного владельца и самостоятельный lifecycle.
* **Реализация:** один владелец на весь репозиторий: `.github/CODEOWNERS`
  (`* @Kvasha62`); каждый контракт несёт `data_ownership.owner`
  (`authorization`, `identity`, `learning`, `records`, `saga`, `tenant_authority`,
  `idempotency_guard`) и `component_version` (SemVer). Самостоятельный lifecycle
  виден в версионировании: `identity 0.3.0` (changelog 0.2.0/0.3.0 в контракте),
  `learning 0.2.0` (Slices 1–4), остальные `0.1.0`; каждый компонент имеет
  собственный state machine (`src/tenant_authority/lifecycle.py`,
  `src/saga/models.py:SAGA_TRANSITIONS`, `src/learning_service/store.py:
  COURSE_STATES`).
* **Тесты:** `tests/test_tenant_lifecycle.py`, `tests/test_saga_state_model.py`,
  `tests/test_tenant_authority_contract.py` (`test_contract_declares_the_lifecycle_of_the_component_itself`).
* **Вывод:** **CONFORMING.**

### LAW-03 — Data Ownership

* **Требование:** компонент является владельцем своих данных.
* **Реализация:** каждый компонент владеет собственным in-memory store логической
  схемы: `src/tenant_authority/store.py` (registry, transitions, audit,
  idempotency), `src/identity_service/store.py` (identities, associations,
  records, audit, idempotency), `src/authorization_service/store.py` (grants,
  services, audit), `src/records_service/store.py`, `src/learning_service/store.py`
  (courses/modules/lessons/assignments/submissions/audit), `src/saga/store.py`
  (workflow state + audit), `src/idempotency/guard.py` (idempotency records).
  Каждая запись несёт `owner_component`/`tenant_id` как fact of the store, а не
  claim вызывающего: `src/learning_service/store.py:register_*` отказывает
  orphan/cross-tenant регистрациям; `src/records_service/engine.py` отказывает
  ресурсу с чужим `owner_component` до вопроса к IS-003.
* **Тесты:** `tests/test_records_ownership.py` (8 тестов),
  `tests/test_learning_authoring.py` (`test_the_store_refuses_orphan_and_cross_tenant_registrations`),
  `tests/test_saga_boundary.py` (`test_the_owned_state_of_a_completed_workflow_holds_no_business_data`).
* **Контракты:** `data_ownership.datasets` с `scope` у всех 7 компонентов.
* **Вывод:** **CONFORMING.**

### LAW-04 — No Direct Database Sharing

* **Требование:** чужие данные доступны только через опубликованные контракты;
  запрещены SQL в чужую БД, импорт внутренних модулей, доступ к внутренним
  таблицам/очередям.
* **Реализация:** БД и SQL в проекте отсутствуют (Level 0, in-memory); все
  межкомпонентные связи идут через опубликованные consumer surfaces
  (`*.reader.*Client` поверх опубликованного HTTP-контракта, исполняемого
  in-process — `src/*/transport.py`). Перекрёстные импорты (полный AST-анализ
  `src/`): `identity_service` импортирует из `tenant_authority` только
  `contracts` и `errors` — опубликованное value-поверхность (в
  `internal_modules` контракта `tenant_authority` это НЕ входит), и ни одного
  внутреннего модуля; `learning_service`/`saga` импортируют из `idempotency`
  только опубликованную поверхность `idempotency.guard`/`idempotency.errors`
  (контракт IS-005: `consumer_surface.module: idempotency.guard`);
  `authorization_service`, `records_service`, `learning_service`, `saga` не
  импортируют `identity_service`, `tenant_authority`, `authorization_service`
  друг друга — ответы провайдера конвертируются в локальные типы
  (`src/*/consumed.py`) адаптерами в точке композиции.
* **Тесты (механические guards):**
  - `tests/test_authorization_dependency_boundary.py` — AST-скан всех модулей
    (включая `importlib`), поведенческая проверка в чистом интерпретаторе,
    control-тест, доказывающий, что сканер находит нарушение;
  - `tests/test_learning_boundary.py` (`test_learning_service_imports_no_other_component`,
    `test_a_fresh_interpreter_imports_no_other_component`);
  - `tests/test_records_bypass_resistance.py`;
  - `tests/test_saga_boundary.py` (`test_the_component_imports_no_business_component_at_runtime`,
    `test_the_only_cross_component_surface_used_is_the_published_is_005_guard`);
  - `tests/test_tenant_context_single_source.py::test_identity_never_imports_tenant_authority_internals`
    (позволены только `contracts`/`errors`/`reader`; `test_tenant_authority_does_not_depend_on_identity`);
  - `tests/test_component_boundary.py` — обход живого графа объектов
    (slots, `__closure__`/`cell_contents`, контейнеры) от опубликованного клиента
    до внутренних типов: application/deployment/engine/store недосягаемы;
    control-обход через транспорт доказывает чувствительность проверки.
* **Вывод:** **CONFORMING.** Одно замечание-нюанс (не нарушение): identity
  использует типы провайдера напрямую, тогда как остальные потребители используют
  локальные `consumed`-типы; допустимо контрактом и охраняемо тестом
  `test_identity_never_imports_tenant_authority_internals`.

### LAW-05 — Contract First

* **Требование:** внешнее взаимодействие определяется контрактом.
* **Реализация:** `components/*/contract/component_contract.json` × 7
  (machine-readable) + `components/*/contract/openapi.yaml` × 7.
* **Тесты:** контрактные тесты сравнивают документ с живыми объектами:
  `tests/test_contract.py`, `tests/test_authorization_contract.py`,
  `tests/test_records_contract.py` (`test_published_api_matches_the_implementation_in_both_directions`),
  `tests/test_learning_contract.py` (25 тестов, включая OpenAPI-схемы ответов и
  error envelope), `tests/test_tenant_authority_contract.py` (15 тестов),
  `tests/test_saga_contract.py` (14 тестов).
* **Вывод:** **CONFORMING** (за исключением одной устаревшей версионной
  декларации — finding CTR-01, раздел C).

### LAW-06 — Independent Evolution

* **Требование:** компонент может развиваться независимо в пределах контрактов.
* **Реализация:** потребители зависят от портов со своим vocabularies
  (`src/*/ports.py`), провайдер скрывается за опубликованным client'ом;
  адаптеры структурные (`src/authorization_service/adapters.py` — «Structural,
  not nominal, on purpose»); версионирование компонентов независимое
  (identity уже 0.3.0, learning 0.2.0, остальные 0.1.0).
* **Тесты:** `tests/test_authorization_dependency_boundary.py::
  test_the_engine_decides_with_ports_that_are_no_component_at_all`
  (решение принимается stub-портами, не знающими провайдеров),
  `tests/test_records_dependency_failure.py` (stub-порт с произвольными ответами).
* **Вывод:** **CONFORMING.**

### LAW-07 — Version Everything That Matters

* **Требование:** компоненты, контракты, схемы и поставляемые платформы
  идентифицируемы и версионируемы.
* **Реализация:** `COMPONENT_VERSION` в `src/*/__init__.py` (совпадает с
  `component_version` контракта и README — проверено для всех 7);
  OpenAPI `info.version` совпадает у 6 из 7 (исключение — identity, DOC-02);
  зависимости версионируются range'ами в `dependencies` контрактов (8 range'ов:
  7 корректны, 1 устарел — CTR-01). Идентификаторы: `uuid4`-основа с видовым
  префиксом (`ten_`, `aud_`, `req_`, `sag_`, `trn_`, `crs_`…) — §11 прямо
  относит конкретный формат UUIDv7/ULID к временным решениям, поэтому текущий
  формат допустим; иммутабельность/непрозрачность подтверждены тестами
  (`tests/test_saga_state_model.py::test_a_saga_id_is_stable_and_cannot_be_reused`).
* **Вывод:** **PARTIAL** — два устаревших версионных поля (CTR-01: saga→identity;
  DOC-02: identity OpenAPI). Нарушения закона нет; декларация версий неполна.

### LAW-08 — Forward Migration

* **Требование:** production schema развивается вперёд; forward-only.
* **Реализация:** production-БД и миграций в репозитории нет (Level 0, in-memory
  stores, `seed_demo`). Механизм миграций не реализован и не требуется до
  появления физической схемы.
* **Тесты:** отсутствуют (неприменимо).
* **Вывод:** **UNPROVEN** (vacuous: правило не нарушено и не проверено — нет
  объекта проверки). Для статуса publishable (§30) миграции обязательны — см.
  DoD-таблицу (DOD-01).

### LAW-09 — Reproducible Platform

* **Требование:** каждая Platform Instance воспроизводима из Manifest.
* **Реализация:** Manifest/Golden Bundle/Composer не существуют — проект в
  standalone/foundation mode до фабричных гейтов (§4.1); README фиксирует это
  («механизмы фабрики ещё не включены … до достижения гейтов это было бы
  нарушением LAW-13»).
* **Вывод:** **UNPROVEN** (механизм отсутствует, но и не требуется в текущем
  режиме — не нарушение §4.1).

### LAW-10 — Certified Composition

* **Требование:** Golden Bundle — сертифицированная комбинация версий.
* **Реализация:** отсутствует (standalone mode, §4.1).
* **Вывод:** **UNPROVEN** (не нарушение).

### LAW-11 — Configuration Before Fork

* **Требование:** кастомизация начинается с configuration и extensions; fork —
  исключение.
* **Реализация:** форков нет; конфигурация декларативна и версионируется с
  компонентом; неизвестный ключ — build error
  (`from_mapping` всех 6 `config.py` поднимает `ConfigurationError`;
  контракты: `additionalProperties: false`).
* **Тесты:** `tests/test_identity_configuration.py`
  (`test_unknown_configuration_key_is_rejected`),
  `tests/test_tenant_authority_contract.py`
  (`test_configuration_schema_rejects_unknown_keys_at_build_time`).
* **Вывод:** **CONFORMING.**

### LAW-12 — Microservice Is Not a Product Boundary

* **Требование:** микросервис — внутренняя реализационная единица.
* **Реализация:** процессных микросервисов нет вообще; один Level 0 modular
  monolith с логическими границами (§5.1); `maturity_level:
  level_0_modular_monolith` во всех 7 контрактах.
* **Вывод:** **CONFORMING.**

### LAW-13 — Complexity Must Be Earned

* **Требование:** распределённость вводится только тогда, когда решает реальную
  проблему; построение каталога и поездов до гейта — нарушение.
* **Реализация:** фабричных механизмов нет (ни каталога, ни registry, ни
  composer, ни bundle); внешних брокеров/очередей/движков нет
  (`src/saga/models.py` non-goals: «no Kafka, RabbitMQ, Redis, Celery, Temporal»;
  `src/idempotency/guard.py` — in-memory bounded guard; события `declared_only`
  с пустыми списками у всех 7 компонентов).
* **Вывод:** **CONFORMING.**

### LAW-14 — Architecture Changes Through ADR

* **Требование:** фундаментальная архитектура изменяется только через ADR;
  каждая RATIFIED версия имеет реальный ADR в `docs/adr/`.
* **Реализация:** ARCHITECTURE.md 1.2.0 → ADR-0010 (RATIFIED, полный реестр
  F-001…F-034, план миграции M0–M6) — **корректно**. ADR-0011
  (SCS-001 Learning Content Authoring Boundary, Level D-пометка документа)
  имеет статус **PROPOSED**, однако его реализация (срез 4: Course/Module/
  Lesson/Assignment authoring, Issue #31) **уже смержена в main**: код
  (`src/learning_service/engine.py:create_course/_create_child/_course_command`,
  `src/learning_service/store.py:publish_course/archive_course`), контракт
  (`components/learning/contract/component_contract.json` v0.2.0, «Slices 1-4»),
  README (`components/learning/README.md`: «Архитектурная основа: … ADR-0011»)
  и 7+ тестовых файлов. Каталог `docs/adr/README.md` знает только ADR-0010
  (DOC-03).
* **Вывод:** **PARTIAL → finding HIGH (ADR-0011)**: процесс ратификации не
  завершён перед реализацией. Закон 1.2.0 самим фактом ADR-0011 не изменён;
  требуется ратификация (решение владельца) и синхронизация каталога.

### LAW-15 — Reversibility

* **Требование:** решения сохраняют возможность безопасной эволюции и консолидации.
* **Реализация:** Level 0 monolith обратимо консолидируется/декомпозируется
  (§4, §5.2); контракты объявляют compatibility policy
  (`additive-minor-breaking-major`); identity 0.2.0 → 0.3.0 выполнена
  additive-изменением без breaking (changelog в контракте).
* **Вывод:** **CONFORMING** (факт: реверсивных блокировок не обнаружено).

### LAW-16 — Tenant Isolation

* **Требование:** tenant-scoped данные чужого арендатора недоступны через прямой
  доступ или контракт без законного tenant-контекста.
* **Реализация (проверка по пунктам задания):**
  - **Источник effective tenant:** только IS-001 —
    `src/identity_service/engine.py:resolve_tenant_context` (из verified
    identity; `TenantContext.source = verified_identity`); authorization/
    learning/records/saga не выводят tenant сами (порты, `src/*/consumed.py`,
    invariant A-002/L-005/R-005/S-009).
  - **Caller-supplied `tenant_id`:** только cross-check —
    `resolve_tenant_context` отклоняет чужой claim (`TENANT_MISMATCH`),
    `X-Tenant-Id`/`payload.tenant_id` передаются как claim и никогда не
    выбирает tenant (`src/records_service/api.py`, `src/learning_service/api.py`).
  - **Cross-tenant access:** ресурсы проверяются по tenant из store, а не из
    запроса (`src/records_service/engine.py:_access` step 1;
    `src/authorization_service/engine.py:_resource_check`); запись другого
    tenant возвращается как unknown (non-disclosure:
    `src/tenant_authority/api.py:PUBLIC_REASONS` — `foreign_tenant` маскируется
    в `tenant_not_found`).
  - **Tenant mismatch:** отказ + аудит
    (`tests/test_tenant_isolation.py::test_key_security_claimed_tenant_b_does_not_become_b`,
    `tests/test_authorization_tenant_context.py::test_a_caller_supplied_tenant_id_can_only_cross_check_never_select`).
  - **Platform/system scope:** registry `tenant_authority` — `platform-scoped`
    (контракт `data_ownership`); кросс-tenant listing разрешён только как
    авторизованная platform-scoped операция `tenant.list` и аудитится
    (`src/tenant_authority/engine.py:list_tenants` — «authorized over
    platform-scoped data only and audited»); foreign Platform Instance
    отклоняется (`_resolve_platform`, `OwnershipDenied`).
  - **Отсутствие второго механизма:** охраняемо тестами
    (`test_tenant_context_single_source.py` — 16 тестов, включая «identity
    holds no copy of the tenant registry», «only tenant authority can change
    tenant state»; «no second mechanism»-тесты у каждого компонента).
  - **Authorization boundary:** lifecycle-gating живое, не snapshot
    (`tests/test_lifecycle_enforcement.py::test_enforcement_is_live_not_a_snapshot_taken_at_context_resolution`,
    `tests/test_authorization_decisions.py::test_an_active_tenant_that_becomes_suspended_is_denied_on_the_next_decision`).
* **Тесты (итого по изоляции):** `tests/test_tenant_isolation.py`,
  `tests/test_platform_ownership.py` (8 тестов), `tests/test_lifecycle_enforcement.py`
  (8 тестов), `tests/test_authorization_tenant_context.py` (9 тестов),
  `tests/test_authorization_security.py`, `tests/test_records_tenant_isolation.py`,
  `tests/test_learning_authoring_security.py` (24 теста cross-tenant),
  `tests/test_saga_acceptance.py` (scenario E — 4 теста).
* **Вывод:** **CONFORMING** — самый доказанный закон: поведение, не только
  декларации.

### LAW-16a — Data Scope Classification

* **Требование:** каждый набор данных имеет ровно одну область; tenant-контекст —
  из проверенной идентичности; caller-tenant — только cross-check.
* **Реализация:** каждый dataset каждого контракта имеет ровно один `scope`
  (проверено по 7 контрактам; `tenant-scoped`: business-данные;
  `platform-scoped`: registry, service_access, audit, idempotency_keys;
  `system-scoped`: нигде не заявлен — допустимо). Смена области неявно
  невозможна: scope — поле контракта версии компонента (LAW-07).
* **Тесты:** `tests/test_tenant_authority_contract.py::
  test_tenant_registry_data_ownership_is_platform_scoped`,
  `tests/test_contract.py` (scopes identity),
  `tests/test_tenant_context_single_source.py::test_identity_contract_declares_the_dependency_and_no_tenant_state_ownership`.
* **Вывод:** **CONFORMING.**

---

## 2. Ключевые правила: глубокая проверка

### 2.1 Ownership / границы (§1.1)

Все пять запрещённых форм проверены:

| Запрет | Факт | Доказательство |
|---|---|---|
| SQL в чужую БД | БД нет (in-memory Level 0) | полный `src/`: нет ни одного драйвера/SQL |
| Импорт внутренних модулей другого компонента | отсутствуют; только опубликованные value-модули/clients | AST-scans + fresh-interpreter tests (§1, LAW-04) |
| Обращение к внутренним таблицам | отсутствует; store объявлены internal в каждом контракте и docstring'ах | `src/*/store.py` docstrings + object-graph walk tests |
| Использование внутренних очередей | очереди не существуют | `src/saga`/`idempotency` non-goals |
| Зависимость от неописанных внутренних структур | отсутствует; все consumed-типы описаны в контрактах (`consumes`, `consumer_surface`) | 7 контрактов + contract tests |

**Вывод: CONFORMING.**

### 2.2 Authentication ≠ Authorization (§6.2)

* Аутентификация: `verify_service_identity` (service identity, §6.2) в
  `tenant_authority`/`authorization`; `verify_identity` (bearer, OIDC-совместимый)
  в `identity`. Ни одна не даёт доступа сама по себе.
* Авторизация: явные permission grants (`authorization_service/store.py:is_granted`
  — «Explicit grant only: no grant, no access»), пермишены сервисов в
  `tenant_authority` (`REGISTRY_PERMISSIONS` vs `LOOKUP_PERMISSIONS`),
  tenant-association пермишены в `identity`.
* **Авторизация выполняется на границе владельца данных:** IS-003 «decides; it
  never enforces» (A-008); финальное enforcement — в `records`/`learning`
  (ENFORCEMENT_CHAIN = ownership → decision → owned-data operation); saga-шаги
  и компенсации проходят ту же границу
  (`tests/test_saga_boundary.py::test_a_compensation_is_authorized_at_the_data_owner_boundary`).
* **Тесты:** `tests/test_conformance.py::test_authentication_does_not_imply_authorization_behaviorally`,
  `tests/test_tenant_authority_security.py::test_authentication_does_not_imply_authorization`,
  `tests/test_authorization_decisions.py::test_an_authenticated_subject_without_permission_is_denied`,
  `tests/test_records_enforcement.py` (6 тестов, включая «denied write never reaches
  the state machine»).

**Вывод: CONFORMING.**

### 2.3 Idempotency (§6.5, AMD-05) — три класса

| Класс | Где | Доказательство replay ≠ retry |
|---|---|---|
| `event_id` (события) | события не публикуются (`declared_only` у всех 7) | N/A (нет событий — декларативно честно) |
| `Idempotency-Key` (API) | learning: все state-changing команды (review, create ×4, publish, archive) — обязательный ключ (`IDEMPOTENCY_KEY_REQUIRED` при отсутствии); tenant_authority: create/transition; identity: PUT records | `tests/test_learning_authoring_lifecycle.py` (exact replay — тот же результат, без второго эффекта; changed binding — `idempotency_conflict`), `tests/test_tenant_authority_idempotency.py` (10 тестов), `tests/test_learning_slice3.py` (review) |
| `command_id` (команды, включая шаги саги) | saga: `saga:<saga_id>:step:<step_id>:<phase>` через IS-005 | `tests/test_saga_idempotency.py` (10 тестов: «a failed attempt records nothing so a retry can execute» — retry ≠ replay; «a replayed step returns the recorded result and applies nothing»), `tests/test_idempotency_guard.py` (11 тестов) |

* Replay отдельная операция: повтор тем же ключом + тем же binding возвращает
  записанный результат без эффекта (`IdempotencyGuard.execute`, inline-реализации
  `tenant_authority/engine.py:_replay`, `identity/engine.py:write_record`);
  конфликт — тот же ключ, другой binding (identity/tenant/operation/fingerprint).
* DENY/сбой зависимости не создают запись (`I-006`;
  `tests/test_idempotency_guard.py::test_authorization_deny_does_not_save_record`).
* **Замечание OBS-01 (LOW):** семантика Idempotency-Key реализована в трёх местах
  (IS-005 + inline в identity/tenant_authority). Каждое объявлено в своём
  контракте как owned data (`idempotency_keys` dataset и т.п.), нарушения нет
  (закон не требует единой реализации), но для LAW-15 это кандидат на
  консолидацию в будущем.

**Вывод: CONFORMING.**

### 2.4 Saga (§6.4, AMD-04)

Все 10 полей минимального контракта задекларированы
(`components/saga/contract/component_contract.json:saga_contract`) и реализованы:

| Поле | Реализация | Тест |
|---|---|---|
| `saga_id` | `SagaContext.saga_id`, уникален, не переиспользуется | `test_a_saga_id_is_stable_and_cannot_be_reused` |
| `correlation_id` | на каждой delivery, в каждом audit event | `tests/test_saga_observability.py` |
| `tenant_id`/scope | неизменяем; из IS-001; mismatch → отказ до шага | `test_saga_acceptance.py` scenario E |
| `step_id` | unique в definition (`SagaDefinition.__post_init__`) | `test_a_step_id_is_unambiguous_inside_one_definition` |
| `state` | closed maps `SAGA_TRANSITIONS`/`STEP_TRANSITIONS`; undeclared → отказ без записи | `tests/test_saga_state_model.py` |
| `retry_policy` | `RetryPolicy(max_attempts, delay)`; retry только declared-retryable отказов | `test_scenario_g_*` |
| `timeout` | `StepDefinition.timeout_seconds`; timeout = failure, не silent success | `test_a_timeout_is_a_failure_and_never_a_silent_success` |
| `compensation` | обязательна в `StepDefinition` до выполнения; сама идемпотентна (свой IS-005 ключ на phase=compensate) | `test_compensation_must_be_declared_before_the_step_can_run`, `test_an_undo_that_already_happened_is_not_applied_again_on_resume` |
| `idempotency` | только через IS-005 (S-010: «no second idempotency mechanism») | `test_the_component_owns_no_second_idempotency_mechanism` |
| `terminal_state` | COMPLETED/COMPENSATED/FAILED; терминальный workflow не принимает delivery | `test_every_driven_workflow_reaches_a_defined_terminal_state`, `test_a_terminal_workflow_accepts_no_further_delivery` |

* **UNPROVEN (заявленная limitation):** timeout оценивается при возврате attempt'а;
  hanging handler в Level 0 не прерывается — зафиксировано честно в контракте
  (`level_0_allowance.stated_limitations`) и docstring `src/saga/executor.py`.
  Не нарушение §6.4 («таймаут не является молчаливым успехом» выполняется), но
  preemption не доказан и не заявлен.
* Cross-component: шаги всегда через опубликованные контракты владельцев
  (composition root); `examples_in_this_repository: components/records/...`.

**Вывод: CONFORMING** (с UNPROVEN-ограничением по preemption, задекларированным).

### 2.5 API (§6.6, §7)

* Формат `/api/v1/...` — у всех 4 HTTP-компонентов (learning —
  `/api/v1/learning/...` через `servers:` в OpenAPI + относительные paths;
  проверено `test_learning_contract.py::test_openapi_uses_servers_and_relative_paths`).
* OpenAPI — у всех 7 компонентов (saga/idempotency — schema-only документы,
  `api.openapi: not_applicable` — согласовано: HTTP-поверхности нет).
* Contract testing — двунаправленное сравление OpenAPI ↔ живые маршруты
  (`test_published_api_matches_the_implementation_in_both_directions` × 4 компонента).
* Versioning: одна API major (v1) у всех — «не более двух major без решения»
  соблюдено; compatibility policy задекларирована у всех 7.
* **Нарушений нет.** Устаревшее поле `info.version` у identity — DOC-02.

**Вывод: CONFORMING** (DOC-02 — LOW).

### 2.6 Observability (§26, AMD-10)

* Standard context генерируется каждым компонентом: `timestamp`, `environment`,
  `platform_id` (deployment-факт, не claim:
  `src/tenant_authority/engine.py:_gate` — «a caller-claimed platform id is
  untrusted input and never lands in the context»), `component_id`,
  `component_version`, `tenant_id`, `request_id`, `trace_id`, `correlation_id`,
  `actor/service_id`. Saga — плюс `saga_id`/`step_id`/`security_sensitive`.
* Health/readiness — у всех 4 HTTP-компонентов (`/health`, `/ready`;
  `test_health_and_readiness_are_published`).
* Aудит: отдельные append-only журналы в каждом компоненте; **каждый** отказ
  (включая schema-отказы до handler'а — `refuse_unreadable_request`) аудитится
  с request/correlation id; креденшелы в аудит не пишутся
  (`tests/test_authorization_audit.py::test_the_audit_journal_is_append_only_from_the_outside`,
  `tests/test_tenant_authority_audit.py::test_audit_never_records_credentials`).
* **UNPROVEN:** metrics, structured logs, distributed traces — отсутствуют
  (применимо к production-компонентам; текущие компоненты — Level 0 demo/proof).
  Контекст не позволяет пересечь tenant boundary: tenant в контексте — только
  resolved-факт, не claim.

**Вывод: CONFORMING для реализованного контура; UNPROVEN — production-метрики/логи/трейсы.**

### 2.7 Security (§27)

* Secrets в коде отсутствуют; demo-креденшелы (`token-*`, `svc-token-*`,
  `authz-svc-token-*`) — фиксированные demo-значения demo-деплоя, объявлены как
  demo в `seed_demo`-docstring'ах; в production-состав не входят (production
  отсутствует).
* Least privilege: service identity с пермишенами только для своей роли
  (`LOOKUP_PERMISSIONS` vs `REGISTRY_PERMISSIONS`; `CONSUMER_PERMISSIONS =
  {authorization.decide}`); «svc_reporting» — аутентифицирован без прав.
* AuthN/AuthZ на всех внешних API — см. 2.2.
* Tenant isolation — см. LAW-16.
* Аудит изменяющих операций и tenant-scoped чтений — есть (все 6 компонентов с
  audit; «allowed_accesses_audited: true» в контрактах records/learning).
* **UNPROVEN:** регулярная проверка зависимостей (нет CI/dependency-audit) — SEC-01.

**Вывод: CONFORMING для реализованного контура; UNPROVEN — dependency-checking.**

---

## 3. Component Contract audit (§6.1, AMD-01)

Обязательные 10 полей: `api`, `events`, `data_export_cdc`, `ui`,
`configuration_schema`, `data_ownership`, `authn`, `authz`,
`compatibility_policy`, `dependencies`.

| Компонент | 10 полей | Соответствие закону | Соответствие коду | Тесты | Замечания |
|---|---|---|---|---|---|
| authorization 0.1.0 | ✓ все 10 | ✓ | ✓ (contract test 15+ тестов) | ✓ | — |
| identity 0.3.0 | ✓ все 10 | ✓ | ✓ (test_contract + context contract) | ✓ | DOC-02: OpenAPI `info.version` 0.1.0 при компоненте 0.3.0 |
| tenant_authority 0.1.0 | ✓ все 10 | ✓ | ✓ (15 contract тестов) | ✓ | — |
| records 0.1.0 | ✓ все 10 | ✓ | ✓ (10 contract тестов) | ✓ | — |
| learning 0.2.0 | ✓ все 10 | ✓ | ✓ (25 contract тестов) | ✓ | ссылка на ADR-0011 PROPOSED (HIGH, ADR-0011) |
| saga 0.1.0 | ✓ все 10 | ✓ | ✓ (14 contract тестов) | ✓ | CTR-01: identity version_range устарел |
| idempotency_guard 0.1.0 | ✓ все 10 | ✓ | ✓ (guard tests 11) | ✓ | NAM-01: `component_id` ≠ имя директории; DOC-05: нет README |

Пополнительные наблюдения:

* `events`/`data_export_cdc` — `declared_only` с пустыми списками у всех 7:
  честно (инфраструктуры нет; «не заявляем то, чего нет» —
  `test_events_and_cdc_are_declared_only_because_no_infrastructure_exists`).
* `configuration_schema` — строгие (additionalProperties: false) и
  соответствующие загрузчикам (contract tests сравнивают со `from_mapping`).
* `consumer_surface` + `internal_modules` + `composition_root_only` —
  задекларированы и охраняемы object-graph/namespace-тестами у всех 4
  клиентских поверхностей.
* `api.openapi: not_applicable` у saga/idempotency согласовано с реальностью
  (HTTP-поверхности нет; OpenAPI-файл фиксирует только value-схемы).

**Вывод: CONFORMING** (замечания — раздел D).

---

## 4. Component DoD (§30)

Требования §30: code; API contract; data ownership; migrations; event contracts;
contract tests; unit/integration tests; deployment artifact; configuration schema;
documentation; health/readiness; version; owner; compatibility.

| Component | Requirement | Evidence | Status |
|---|---|---|---|
| authorization | code | `src/authorization_service/` (14 модулей) | ✓ |
| authorization | API contract | `components/authorization/contract/{component_contract.json,openapi.yaml}` | ✓ |
| authorization | data ownership | контракт `data_ownership` + `store.py` | ✓ |
| authorization | migrations | отсутствуют (in-memory) | ✗ PARTIAL |
| authorization | event contracts | `events: declared_only, published: []` | ~ PARTIAL (событий нет — задекларировано) |
| authorization | contract tests | `test_authorization_contract.py` (15+ тестов) | ✓ |
| authorization | unit/integration tests | 9 тестовых файлов (decisions/security/audit/…) | ✓ |
| authorization | deployment artifact | `deployment.py` = композиция (engine+store+app), **не** deployable артефакт (нет образа/digest) | ~ PARTIAL |
| authorization | configuration schema | `config.py` + контракт; unknown key → error | ✓ |
| authorization | documentation | `components/authorization/README.md` (250 строк) | ✓ |
| authorization | health/readiness | `/health`, `/ready` в `api.py` | ✓ |
| authorization | version | 0.1.0 в `__init__`/контракте/README | ✓ |
| authorization | owner | CODEOWNERS + контракт | ✓ |
| authorization | compatibility | `compatibility_policy` в контракте | ✓ |
| identity | code / API contract / data ownership / contract tests / tests / config / docs / health / version / owner / compatibility | как у authorization (аналогично) | ✓ (11 из 14) |
| identity | migrations / event contracts / deployment artifact | отсутствуют / declared_only / `create_app`-фабрика без deployable артефакта (у identity нет `deployment.py` — композиция в composition root) | ✗ / ~ / ~ |
| tenant_authority | все 14 пунктов | как у authorization | ✓ 11, ✗1 (~2) |
| records | все 14 пунктов | как у authorization | ✓ 11, ✗1 (~2) |
| learning | все 14 пунктов | как у authorization (API: 10 операций) | ✓ 11, ✗1 (~2) |
| saga | code / contract / ownership / contract tests / tests / config / docs / version / owner / compatibility | `src/saga/` (7 модулей), контракт, README | ✓ |
| saga | migrations / event contracts / deployment artifact / health-readiness | отсутствуют / declared_only / нет (`SagaExecutor` — library) / нет (HTTP-поверхности нет — N/A, задекларировано) | ✗ / ~ / ~ / ~ N/A |
| idempotency | code / contract / ownership / contract tests / tests / config / version / owner / compatibility | `src/idempotency/`, контракт | ✓ |
| idempotency | migrations / events / deployment artifact / docs / health | отсутствуют / declared_only / нет (library) / **README отсутствует** / N/A (library) | ✗ / ~ / ~ / ✗ / ~ N/A |

**Итог §30:** ни один компонент не является `publishable` по §30 («Отсутствие
обязательной части означает NOT PUBLISHABLE») — блокируют migrations,
deployable artifact и event-инфраструктура. Проект **не заявляет** публикацию
компонентов (standalone/foundation mode, §4.1), поэтому это — **допустимое
промежуточное состояние**, а не нарушение; но статус NOT PUBLISHABLE должен
быть зафиксирован в документации (см. G-06).

---

## 5. Tests как доказательство архитектуры

| Architecture rule | Implementation | Test | Status |
|---|---|---|---|
| Component boundaries / foreign imports | ports+adapters+`consumed`, reader-клиенты, transport channels | `test_authorization_dependency_boundary.py` (AST + fresh interpreter + control), `test_learning_boundary.py`, `test_records_bypass_resistance.py`, `test_saga_boundary.py`, `test_tenant_context_single_source.py::test_identity_never_imports_tenant_authority_internals` | CONFORMING |
| Foreign DB/store access | store — internal; published client — values only | `test_component_boundary.py` (object-graph walk, 30+ тестов), `test_records_bypass_resistance.py`, `test_authorization_boundary.py` | CONFORMING |
| Single source of tenant context | IS-001 `resolve_tenant_context` | `test_tenant_context_single_source.py` (16 тестов), `test_conformance.py::test_tenant_context_source_is_verified_identity_not_caller_claim` | CONFORMING |
| Caller `tenant_id` = cross-check | `X-Tenant-Id`/payload → mismatch refusal | `test_tenant_isolation.py`, `test_authorization_tenant_context.py`, `test_learning_slice2.py::test_caller_supplied_tenant_id_cannot_select_the_tenant_for_list`, `test_platform_ownership.py::test_claimed_platform_id_is_only_a_cross_check` | CONFORMING |
| Cross-tenant isolation | store-fact tenant vs subject tenant | `test_authorization_decisions.py::test_a_subject_of_one_tenant_never_reaches_a_resource_of_another`, `test_records_tenant_isolation.py`, `test_learning_authoring_security.py` (24 теста), `test_saga_acceptance.py` scenario E | CONFORMING |
| Platform ownership (foreign instance) | `platform_id` check, non-disclosure | `test_platform_ownership.py` (8 тестов), `test_tenant_authority_security.py::test_error_payloads_do_not_disclose_tenant_existence_across_platforms` | CONFORMING |
| authn ≠ authz | permission grants, explicit deny | `test_conformance.py`, `test_tenant_authority_security.py`, `test_authorization_decisions.py` | CONFORMING |
| Authorization at data-owner boundary | ENFORCEMENT_CHAIN, fail closed | `test_records_enforcement.py`, `test_records_dependency_failure.py` (7 тестов malformed/non-authoritative answers), `test_learning_boundary.py::test_nonauthoritative_answers_fail_closed` | CONFORMING |
| Fail closed (dependency fault) | `except Exception → AUTHORITY_UNAVAILABLE/DENY` | `test_records_dependency_failure.py`, `test_component_boundary.py::test_a_broken_contract_answer_fails_closed_instead_of_granting_access`, `test_authorization_security.py::test_an_unreachable_tenant_authority_denies_instead_of_allowing` | CONFORMING |
| Malformed dependency answers | closed reason vocabulary, unknown → unavailable | `test_records_dependency_failure.py::test_a_non_authoritative_answer_fails_closed_including_permissive_ones`, `test_authorization_tenant_context.py::test_an_unknown_identity_answer_fails_closed` | CONFORMING |
| Malformed requests audited | `refuse_unreadable_request` | `test_authorization_malformed_requests.py` (9 тестов), `test_records_audit.py::test_unreadable_requests_are_audited_without_their_payload_values` | CONFORMING |
| Idempotency (3 класса, replay≠retry) | IS-005 guard, inline replay, saga command ids | `test_idempotency_guard.py` (11), `test_tenant_authority_idempotency.py` (10), `test_saga_idempotency.py` (10), slice3/authoring lifecycle | CONFORMING |
| Saga (state/terminal/compensation/timeout) | closed maps, IS-005 delivery | `test_saga_state_model.py` (18), `test_saga_acceptance.py` (17 сценариев A–H), `test_saga_observability.py` (10) | CONFORMING (UNPROVEN: preemption hanging handler — задекларировано) |
| Immutable review/snapshot facts | `apply_review` immutable; frozen dataclasses | `test_learning_slice3.py::test_review_fact_is_immutable_across_different_keys`, `test_component_boundary.py::test_values_cross_the_boundary_not_live_records` | CONFORMING |
| Immutable published hierarchy | publish/archive atomic, no unpublish/unarchive | `test_learning_authoring_lifecycle.py` (24), `test_learning_authoring_concurrency.py` (9) | CONFORMING |
| Audit (append-only, all denials) | journal in each store | `test_authorization_audit.py` (7), `test_records_audit.py` (8), `test_tenant_authority_audit.py` (8), `test_saga_observability.py` | CONFORMING |
| Observability context | `observability()` в каждом engine | `test_tenant_authority_observability.py` (8), `test_authorization_audit.py::test_the_observability_context_is_the_standard_one` | CONFORMING |
| Lifecycle enforcement | state machine + per-operation policy | `test_lifecycle_enforcement.py` (8), `test_tenant_registry.py` | CONFORMING |
| Configuration (unknown key = build error) | `from_mapping` × 6 | `test_identity_configuration.py`, `test_tenant_authority_contract.py` | CONFORMING |
| Contract ↔ implementation | openapi/contract vs live app | `test_*_contract.py` × 6 + `test_contract.py` | CONFORMING |

Правила **без** автоматизированного доказательства (не объявляются CONFORMING):
LAW-08 (migrations — нет объекта), LAW-09/10 (manifest/bundle — standalone),
§26 metrics/logs/traces (production-level), §27 dependency-checking,
§9 schema-registry status'ы, §12 CI migration path. — См. раздел E.

**Итого: 730 passed / 621 test-функция / 0 failed.** Покрытие всех
существенных законов для реализованного контура — доказательное, не
декларативное.

---

## 6. Фактический dependency graph

```text
                        (composition root: tests/conftest.py — единственный узел,
                        знающий несколько компонентов; задекларирован контрактами)

identity ──────────api: lookup, lifecycle_decision─────────────► tenant_authority
   │
   │ api: resolve_context (IdentityContextClient)
   ▼
authorization ◄──────────────────────────────────────────────────┐
   ▲   ▲        (api: lifecycle_decision — TenantAuthorityClient) │
   │   │                                     tenant_authority ────┘
   │   └─────── api: decide (AuthorizationClient)
   │
   │ api: resolve_context (create-course only)
   ▼
learning ──internal-consumer-surface: IdempotencyGuard.execute──► idempotency
saga ─────api: resolve_context (TenantContextPort)──────────────► identity
saga ─────internal-consumer-surface: IdempotencyGuard.execute───► idempotency
saga (steps, runtime wiring by composition root) ──published contract──► records / любой data owner
```

| Зависимость | Механизм | Объявлена в контракте | Public API | Internal модули | Hidden dependency | §18 |
|---|---|---|---|---|---|---|
| identity → tenant_authority | published client (reader) поверх HTTP-контракта, in-process Level 0 | ✓ `dependencies` (kind: api) | ✓ `contracts`/`errors` — опубликованная value-поверхность (не в `internal_modules`) | ✗ отсутствуют | ✗ (охраняемо тестом) | ✓ |
| authorization → identity | published client + адаптер → локальные `consumed`-типы | ✓ (kind: api, `consumes`) | ✓ | ✗ (AST + fresh interpreter) | ✗ | ✓ |
| authorization → tenant_authority | published client + адаптер | ✓ (kind: api, `consumes`) | ✓ | ✗ (AST + fresh interpreter) | ✗ | ✓ |
| records → authorization | published client + адаптер | ✓ (kind: api) | ✓ | ✗ (AST) | ✗ | ✓ |
| learning → authorization | published client + адаптер | ✓ (kind: api) | ✓ | ✗ (AST) | ✗ | ✓ |
| learning → identity | published client (только create-course) | ✓ (kind: api, с обоснованием) | ✓ | ✗ (AST) | ✗ | ✓ |
| learning → idempotency_guard | `IdempotencyGuard` (опубликованная модульная поверхность) | ✓ (kind: internal-consumer-surface) | ✓ (`consumer_surface.module: idempotency.guard`) | ✗ | ✗ | ✓ |
| saga → identity | published client через `TenantContextPort` | ✓ (kind: api) — **версионный range устарел (CTR-01)** | ✓ | ✗ (AST + runtime) | ✗ | ✓ (с декларативным дефектом) |
| saga → idempotency_guard | `IdempotencyGuard` | ✓ (kind: internal-consumer-surface) | ✓ | ✗ | ✗ | ✓ |
| saga → (data owners, шаги) | runtime-подключение composition root к опубликованным контрактам | ✓ `consumed_at_runtime_by_steps` | ✓ | ✗ (runtime-тест) | ✗ | ✓ |
| tenant_authority → identity | **отсутствует** (направление запрещено и охраняемо) | ✓ (`dependencies: []`) | — | ✗ (`test_tenant_authority_does_not_depend_on_identity`) | ✗ | ✓ |

**Вывод: CONFORMING §18** — каждая зависимость объявлена, версионируется
(за одним устаревшим range), проверяется (guards + contract tests), трассируется
до конкретного контракта. Hidden dependencies: не обнаружено.

---

## B. Conformance Matrix (master)

| ID | Architecture rule | Evidence | Status | Severity | Action |
|---|---|---|---|---|---|
| LAW-01 | Business Boundary | README/контракты 7 компонентов; non-goals в коде | CONFORMING | — | — |
| LAW-02 | Component Ownership | CODEOWNERS; owner+version в контрактах; lifecycle-модели | CONFORMING | — | — |
| LAW-03 | Data Ownership | `src/*/store.py`; `data_ownership` в 7 контрактах; tests | CONFORMING | — | — |
| LAW-04 | No Direct DB Sharing | AST/fresh-interpreter guards; object-graph walks; no SQL | CONFORMING | — | — |
| LAW-05 | Contract First | 7×(contract JSON + OpenAPI); contract tests | CONFORMING | LOW (CTR-01, DOC-02) | G-05, G-09 |
| LAW-06 | Independent Evolution | ports/adapters/consumed; stub-port tests | CONFORMING | — | — |
| LAW-07 | Version Everything | SemVer у 7; range'ы зависимостей | PARTIAL | MEDIUM | G-05 (saga range) |
| LAW-08 | Forward Migration | миграций нет (in-memory L0) | UNPROVEN | MEDIUM (DOD-01) | зафиксировать NOT PUBLISHABLE |
| LAW-09 | Reproducible Platform | standalone §4.1; механизмов нет | UNPROVEN | — | до фабричных гейтов — не нарушение |
| LAW-10 | Certified Composition | Golden Bundle нет; standalone §4.1 | UNPROVEN | — | — |
| LAW-11 | Configuration Before Fork | no forks; config strict; unknown key = build error | CONFORMING | — | — |
| LAW-12 | Microservice ≠ Product | Level 0 monolith, `maturity_level` в контрактах | CONFORMING | — | — |
| LAW-13 | Complexity Earned | no factory mechanics/brokers/queues; non-goals | CONFORMING | — | — |
| LAW-14 | Changes Through ADR | ADR-0010 RATIFIED ✓; ADR-0011 PROPOSED при смерженной реализации | PARTIAL | HIGH | G-12 (ратификация), G-08 |
| LAW-15 | Reversibility | additive-эволюция 0.2→0.3; L0 консолидируемо | CONFORMING | — | OBS-01 — наблюдение |
| LAW-16 | Tenant Isolation | IS-001 single source; cross-check; isolation tests | CONFORMING | — | — |
| LAW-16a | Data Scope Classification | scope у каждого dataset; platform-scoped listing audited | CONFORMING | — | — |
| §5.4 | scopes + tenant context | реализация+тесты (LAW-16/16a) | CONFORMING | — | — |
| §6.1 | Contract 10 fields | 7/7 контрактов полны; contract tests | CONFORMING | LOW | NAM-01, DOC-05 |
| §6.2 | AuthN ≠ AuthZ; enforcement at owner | engine chains; tests | CONFORMING | — | — |
| §6.3–6.4 | Events/Saga | saga: 10 полей; declared_only events | CONFORMING | — | UNPROVEN: preemption (заявлено) |
| §6.5 | Idempotency 3 класса | IS-005 + inline (задекларировано); replay≠retry tests | CONFORMING | LOW | OBS-01 — наблюдение |
| §6.6/§7 | API + versioning | /api/v1 × 4; OpenAPI × 7; contract tests | CONFORMING | LOW | DOC-02 |
| §9 | Schema Registry (L0) | config schemas in contracts; status'ы не моделированы | PARTIAL | LOW | — |
| §10 | Contract Testing | implemented per component | CONFORMING | — | — |
| §11 | Identifiers | uuid4+prefix (временный формат, допустим §11); immutability tests | CONFORMING | — | — |
| §12 | Migrations | отсутствуют; CI migration path не применим | UNPROVEN | MEDIUM | DOD-01 |
| §14–17 | Bundle/Matrix/Manifest/Composer | standalone §4.1 — не введены | UNPROVEN | — | до гейтов — не нарушение |
| §18 | Dependency Graph | все связи объявлены/public/проверены | CONFORMING | MEDIUM | CTR-01 |
| §20 | Configuration | strict loaders + tests | CONFORMING | — | — |
| §26 | Observability | standard context + audit; metrics/logs/traces нет | CONFORMING (контур) / UNPROVEN (production) | LOW | — |
| §27 | Security | authn/authz/isolation/audit; dependency-checking нет | CONFORMING (контур) / UNPROVEN (deps) | MEDIUM | SEC-01, G-04 |
| §30 | Component DoD | 11/14 пунктов у каждого; migrations/artifact/events не закрыты | PARTIAL | MEDIUM | DOD-01/02 |
| §34 | ADR process | ADR-0010 ✓; ADR-0011 PROPOSED; каталог не обновлён | PARTIAL | HIGH | G-08, G-12 |
| §4.1 | Standalone gate compliance | factory mechanics не введены | CONFORMING | — | — |
| §42 | Conformance checks | tests + quality-gate.ps1; gate красный; CI нет | PARTIAL | HIGH | G-01…G-04 |

---

## C. Critical findings

### CRITICAL

Нарушений критической серьёзности (прямой доступ к чужим данным, bypass
authorization, сломанная tenant isolation, незаявленный breaking change,
нарушенный ADR-процесс ратифицированного закона) **не обнаружено**.

### HIGH

**H-1 (QG-01) — Quality Gate красный и нерепродуктивный.**
`scripts/quality-gate.ps1` выполняет `compileall` → `ruff check .` →
`black --check .` → `pytest`. Факт на commit 8445e2b:
`ruff check .` = **FAIL, 97 errors** (ruff 0.16.6, default rule set;
breakdown: B023×26, BLE001×25, F401×13, TRY004×11, S112×6, S110×4, B017×3,
PLW1510×2, RET501×2, PLR1711×2, B008×1, SIM114×1, PLR0402×1; 19 auto-fixable);
`black --check .` = **FAIL, 67 of 131 files** (одинаково для black 25.9.0 и
26.5.1; 429 строк длиннее 88 символов; `[tool.black]` не задекларирован).
`pyproject.toml` declares dev = `[pytest>=8.3.0]` — ruff и black **не являются
заявленными зависимостями**, ни версий, ни конфигураций (`[tool.ruff]`/
`[tool.black]` отсутствуют): результат gate зависит от версии инструментов на
машине. Механизм проверки исполнения закона (§42, AMD-12) существует
(scripts + tests), но в текущем состоянии **не проходит на собственном main**.
Файлы: `scripts/quality-gate.ps1:38-52`, `pyproject.toml`.
Действие: G-01…G-04 (Level A; не требует ADR).

**H-2 (ADR-0011) — реализация смержена при PROPOSED ADR.**
`docs/adr/ADR-0011-…md` имеет `Status: PROPOSED` (дата 2026-09-09), при этом
его предмет (SCS-001 Learning Content Authoring, срез 4, Issue #31) полностью
реализован в `main`: `src/learning_service/engine.py` (create_course,
create_module/lesson/assignment, publish_course, archive_course, read_course),
`src/learning_service/store.py` (hierarchy critical section, atomic
publish/archive), контракт v0.2.0 (Slices 1-4), `components/learning/README.md`
(«Архитектурная основа: … ADR-0011»), 7 тестовых файлов (slice3/slice4/
authoring*). Это разрыв процесса LAW-14/§34: существенное решение уровня
компонента зафиксировано ADR'ом, но не ратифицировано до реализации.
Действие: ратификация ADR-0011 владельцем (G-12) + обновление каталога (G-08).
Архитектурный текст 1.2.0 не изменяется.

**H-3 (DOC-01) — DOCUMENTATION_BASELINE устарел и противоречит фактам.**
`docs/DOCUMENTATION_BASELINE.md`:
- §2 таблица: «Реализация компонентов | код соответствующего компонента |
  **ещё отсутствует**» — факт: 7 компонентов реализованы в `src/` (16 133 строки);
- §5: «ещё не созданы: … production contracts/schemas; … contract-test suite;
  CI/conformance automation» — факт: machine-readable Component Contracts
  существуют (`components/*/contract/`), contract-test suite существует
  (6+ файлов contract/boundary-тестов); CI-automation по-прежнему отсутствует,
  но quality-gate скрипт появился;
- §4: «Component Contracts; contract-test suite; CI/conformance automation»
  перечислены как «ожидаемые по мере развития» — они уже появились.
Документ — канонический record состояния документации (§2 OPERATING_MODEL),
его фактические утверждения должны отражать репозиторий. Не является нарушением
архитектуры, но — DOCUMENTATION GAP высокой значимости. Действие: G-06 (Level A).

### MEDIUM

**M-1 (CTR-01) — saga: устаревший версионный range зависимости identity.**
`components/saga/contract/component_contract.json` → `dependencies`:
`identity: ">=0.1.0,<0.2.0"`. Фактический identity = **0.3.0**, и saga
использует именно 0.3.0-поверхность (`GET /api/v1/context` /
`IdentityContextClient.resolve_context` — добавлена в identity 0.3.0 по
changelog контракта). Объявленная декларация противоречит фактически
используемой версии (LAW-07, §7, §18 «каждая зависимость объявляется,
версионируется»). Проверка версионными range'ами по всем 9 зависимостям:
8 корректны, эта одна — нет. Действие: G-05 (Level A, исправление
декларации до факта).

**M-2 (CI-01) — отсутствие CI.**
`.github/` содержит только `CODEOWNERS`; workflow'ов нет. Quality Gate
выполняется только локально (PowerShell). При OPERATING_MODEL («GitHub —
единственный источник истины», завершение = merge) и §42 («для каждого
правила, которое можно проверить машинно, должна существовать automated check»)
проверки существуют (tests + script), но не автоматизированы в delivery flow.
Действие: G-04 (Level A операционное).

**M-3 (DOD-01) — migrations отсутствуют у всех 7 компонентов.**
Обязательный пункт DoD §30; для publication — блокирующий. В standalone-режиме
не является нарушением (БД физически нет, Level 0). Действие: зафиксировать
статус NOT PUBLISHABLE в обновлённом baseline (G-06).

**M-4 (DOD-02) — deployment artifact = композиционный код.**
`src/*/deployment.py` (tenant_authority/authorization/records/learning)
собирает engine+store+app — это composition-internal объект, а не
поставляемый артефакт (нет образа, digest'а, deployment-спецификации).
По §30 пункт «deployment artifact» закрыт PARTIAL. Действие: при движении к
публикации — реальный артефакт (требует решения уровня B, не ADR Level C/D).

**M-5 (SEC-01) — механизм регулярной проверки зависимостей отсутствует.**
§27: «Зависимости регулярно проверяются». Нет CI, нет dependency-audit'а,
нет lock-файла (в репозитории нет `uv.lock`/`pip freeze`; версии внешних
зависимостей — только минимальные bounds в `pyproject.toml`). UNPROVEN —
нарушение не доказано (production-состав отсутствует), но и доказательств
нет. Действие: G-04 + lock-файл при введении CI.

### LOW

**L-1 (DOC-02):** `components/identity/contract/openapi.yaml` —
`info.version: 0.1.0` при `component_version: 0.3.0` (документ при этом
включает 0.3.0-endpoint `/api/v1/context` — противоречие внутри документа).
**L-2 (DOC-03):** `docs/adr/README.md` «Текущее подтверждённое состояние»
упоминает только ADR-0010; ADR-0011 (PROPOSED) в каталоге не зафиксирован.
**L-3 (DOC-04):** `README.md` «Статус» описывает только foundation stage и не
упоминает реализованные компоненты/сlices (противоречий нет, но картина
неполная).
**L-4 (NAM-01):** `components/idempotency/` (директория, пакет `idempotency`)
vs `component_id: "idempotency_guard"` в контракте — несогласованная идентичность.
**L-5 (DOC-05):** `components/idempotency/` без README (у 6 из 7 компонентов README есть; DoD «documentation»).
**L-6 (SYM-01):** `src/tenant_authority/api.py:297-300` — module-level demo
deployment + `app` (единственный api-модуль с живым module-level объектом;
другие публикуют только `create_app`). Demo задекларирован, нарушение
отсутствует; асимметрия — кандидат на G-11 (consistency).
**L-7 (OBS-01):** Idempotency-Key-семантика реализована в трёх местах (IS-005,
inline identity `write_record`, inline tenant_authority `_replay`/
`_store_idempotency`). Все три — owned data соответствующих компонентов,
задекларированы в их контрактах; нарушения нет; кандидат на консолидацию (LAW-15).
**L-8 (LINT):** B023 (loop-variable lambdas) в
`tests/test_learning_authoring_concurrency.py` (25) и
`src/saga/executor.py:494` — позднего связывания фактически нет (lambda
консумируется в той же итерации, проверено по коду: `race()` вызывается до
следующей итерации; `step.execute(context)` — немедленный вызов), исправление
— default-argument binding; F401 (13) — мёртвые импорты, включая
`datetime.timezone` × 6 в `src/*/engine.py`. Часть H-1 (G-01).

---

## D. Documentation gaps

| # | Документ | Устаревшее/отсутствующее | Факт | Действие |
|---|---|---|---|---|
| DOC-01 (HIGH) | `docs/DOCUMENTATION_BASELINE.md` §2 (таблица: «Реализация компонентов … ещё отсутствует»), §4 (Component Contracts / contract-test suite «по мере развития»), §5 («ещё не созданы: … production contracts/schemas; contract-test suite; …») | 7 компонентов в `src/`; 7 контрактов; contract-test suite; quality-gate скрипт | G-06: привести baseline к факту (сохранить `CLEAN`-модель, обновить фактические строки: реализация — есть (Level 0 proof slices), контракты — есть, contract tests — есть, migrations / factory mechanics / CI — отсутствуют) |
| DOC-02 (LOW) | `components/identity/contract/openapi.yaml` `info.version: 0.1.0` | component 0.3.0; 0.3.0-endpoint уже в документе | G-09: bump до 0.3.0 |
| DOC-03 (LOW) | `docs/adr/README.md` «Текущее подтверждённое состояние» — только ADR-0010 | ADR-0011 существует (PROPOSED) | G-08: добавить ADR-0011 со статусом PROPOSED |
| DOC-04 (LOW) | `README.md` «Статус» — только foundation stage, компоненты не упомянуты | 7 компонентов, SCS-001 Slices 1–4 | G-07: обновить статус (standalone-режим без изменений) |
| DOC-05 (LOW) | `components/idempotency/` — нет README | у 6 других компонентов README есть (DoD «documentation») | G-10: добавить README |
| DOC-06 (в составе H-2) | `components/learning/README.md`, learning-контракт ссылаются на ADR-0011 как на «Архитектурную основу» при статусе PROPOSED | ADR-0011 PROPOSED | G-12: ратификация (решение владельца); до ратификации — пометить «PROPOSED» |
| DOC-07 (в составе M-1) | `components/saga/contract/component_contract.json` identity range | фактический identity 0.3.0 | G-05: `>=0.3.0,<0.4.0` |
| DOC-08 | `pyproject.toml` — ruff/black не объявлены как dev-зависимости и не сконфигурированы | gate их вызывает | G-03 (рекомендация — требует одобрения перед применением, §8 задания) |

Принцип «не исправлять созданием нового архитектурного закона» соблюдён:
все действия G-xx — приведение документов/деклараций к факту (Level A), ни одно
не меняет LAW-01…16, §-нормативов 1.2.0, контрактов совместимости или
ownership-модели.

---

## E. Unproven claims (не считать доказанными)

1. **LAW-09/§16–17 (Manifest/Composer/Golden Bundle, reproducibility):**
   механизмов нет; standalone-режим §4.1 допускает это, но воспроизводимость
   платформенного уровня **не доказана** (и не заявлена).
2. **LAW-08/§12 (migrations, CI-проверка пути N-3→N):** нет миграций и CI —
   правило ни нарушено, ни доказано.
3. **§26 production-observability (metrics, structured logs, distributed
   traces):** отсутствует; текущие компоненты — Level 0 proof/demo, не
   production. Стандартный context + audit доказаны, production-стек — нет.
4. **§27 «зависимости регулярно проверяются»:** механизма нет (нет CI/
   dependency-audit/lock-файла) — UNPROVEN (SEC-01).
5. **§9 Schema Registry status'ы** (`draft/active/deprecated/retired`):
   не моделированы (config schemas есть, статусов нет).
6. **Saga timeout preemption:** задекларированная limitation Level 0
   (контракт `level_0_allowance.stated_limitations`): hanging handler не
   прерывается; timeout оценивается при возврате attempt'а. «Timeout — не
   silent success» доказано, preemption — нет.
7. **Event delivery (at-least-once), tolerant reader, dual-publish:** событий
   нет (`declared_only`) — правила §6.3/§8 неприменимы и недоказаны.
8. **Cross-process recovery саги:** явно out-of-scope (контракт),
   недоказуемо в текущем режиме.
9. **Publishability компонентов (§30):** НЕ доказана — миграции, deploy
   artifact, event-инфраструктура отсутствуют; проект публикацию не заявляет.
10. **Ратификация ADR-0011:** не доказана (статус PROPOSED) — см. H-2.

Ни одно из перечисленного не объявлено CONFORMING и не используется как
доказательство соответствия.

---

## F. ADR-required changes

**Изменений, требующих нового ADR (Level C/D), данным аудитом не выявлено.**

Ни один доказанный дефект не требует изменения: Component definition, Data
ownership, Contract model, Versioning, Migration policy, Manifest model,
Golden Bundle, Fork policy, maturity levels, tenant isolation или
LAW-01…LAW-16. В частности:

* ратификация ADR-0011 — это **завершение существующего ADR-процесса**
  (решение владельца), а не новый ADR и не изменение текста 1.2.0;
* исправления G-01…G-11 — Level A (реализация/документация/инструменты);
* возможная будущая консолидация трёх реализаций Idempotency-Key (OBS-01) —
  решение уровня B (ADR компонента, если будет признано существенным);
* реальный deploy artifact (DOD-02) — решение уровня B при движении к
  публикации.

**STOP-проверка выполнена:** ни одно обнаруженное состояние не требует
«исправить архитектуру, чтобы исправить реализацию» — реализация соответствует
закону; расхождения — в декларациях и процессах.

---

## G. Implementation fixes (разрешены без ADR)

| # | Исправление | Файлы | Уровень | Замечание |
|---|---|---|---|---|
| G-01 | Устранить 97 ruff-нахождений честно: B023 — default-argument binding loop-переменных; F401 — удалить мёртвые импорты (включая `datetime.timezone` × 6 в `src/*/engine.py`); BLE001/TRY004/S110/S112/B017/PLR0402/RET501/PLR1711/SIM114/B008/PLW1510 — сузить обработчики там, где возможно (fail-closed `except Exception` в адаптерах — осознанный архитектурный паттерн: для него — **явная** конфигурация `[tool.ruff]` с задокументированным пер-file/per-line обоснованием, а не тихое отключение) | `src/**`, `tests/**`, `pyproject.toml` | A | без отключения правил «втихую» |
| G-02 | Задекларировать формат-конфигурацию и привести формат: `black --check .` сейчас = 67 files; либо зафиксировать line-length в `[tool.black]` и переформатировать | `pyproject.toml`, `src/**`, `tests/**` | A | решение line-length — операционное |
| G-03 | **Рекомендация (применять после явного одобрения — dependency policy):** в `pyproject.toml` → `[project.optional-dependencies] dev` добавить `ruff`, `black` (pinned) + `[tool.ruff]`/`[tool.black]`-секции; зафиксировать lock-файл | `pyproject.toml` | A (policy-решение) | по заданию — сначала finding, потом изменение |
| G-04 | Подключить quality gate в GitHub CI (workflow, запуск compileall/ruff/black/pytest на PR и в main) + lock-файл для воспроизводимости | `.github/workflows/` | A | закрывает M-2, часть SEC-01 |
| G-05 |Saga-контракт: identity `version_range: ">=0.3.0,<0.4.0"` (к факту) | `components/saga/contract/component_contract.json` | A | закрывает CTR-01 |
| G-06 | Обновить `DOCUMENTATION_BASELINE.md` к факту: реализация компонентов — есть (7 компонентов, Level 0, SCS-001 Slices 1–4); Component Contracts — есть; contract-test suite — есть; quality-gate — есть; migrations / factory mechanics / CI — отсутствуют; зафиксировать NOT PUBLISHABLE по §30 | `docs/DOCUMENTATION_BASELINE.md` | A | закрывает DOC-01, DOD-01 |
| G-07 | README «Статус»: упомянуть реализованные компоненты/сlices; standalone-режим без изменений | `README.md` | A | закрывает DOC-04 |
| G-08 | Каталог ADR: ADR-0011 (PROPOSED) в «Текущее подтверждённое состояние» | `docs/adr/README.md` | A | закрывает DOC-03 |
| G-09 | Identity OpenAPI `info.version` → 0.3.0 | `components/identity/contract/openapi.yaml` | A | закрывает DOC-02 |
| G-10 | Добавить `components/idempotency/README.md` | `components/idempotency/README.md` | A | закрывает DOC-05 |
| G-11 | (опционально, consistency) убрать module-level demo `app` из `src/tenant_authority/api.py` (или задокументировать его как demo-вход в README компонента) | `src/tenant_authority/api.py` | A | закрывает L-6 |
| G-12 | **Решение владельца:** ратификация ADR-0011 (или откат/переоформление среза 4) — до ратификации пометка PROPOSED в learning-документах | `docs/adr/ADR-0011-…md` | процесс | закрывает H-2 |

Порядок применения: G-05/G-06/G-07/G-08/G-09/G-10 (док-синхронизация) →
G-01/G-02/G-03 (tooling) → G-04 (CI) → G-11/G-12.

---

## H. Quality Gate — фактический статус

Скрипт: `scripts/quality-gate.ps1` — проверяет Python 3.13, затем
`compileall src` → `ruff check .` → `black --check .` → `pytest tests/`.

| Шаг | Фактический статус (commit 8445e2b) | Детали |
|---|---|---|
| `python -m compileall -q src` | **PASS** | все модули компилируются |
| `ruff check .` | **FAIL — 97 errors** (19 auto-fixable) | ruff 0.16.6, default rule set; breakdown: B023×26, BLE001×25, F401×13, TRY004×11, S112×6, S110×4, B017×3, PLW1510×2, RET501×2, PLR1711×2, B008×1, SIM114×1, PLR0402×1. Подмножество «классических» E4/E7/E9/F — 24 errors. `[tool.ruff]` отсутствует — набор правил и результат зависят от версии ruff |
| `black --check .` | **FAIL — 67 of 131 files** | идентично для black 25.9.0 и 26.5.1 (не дрейф версий, а состояние кода: 429 строк > 88 символов; `[tool.black]` отсутствует) |
| `python -m pytest tests/` | **PASS — 730 passed** | 0 failed, 0 error, 2 warnings (deprecations сторонних пакетов) |
| `quality-gate.ps1` целиком | **FAIL** | падает на шаге Ruff (до Black/Test suite). Дополнительно: gate требует ровно Python 3.13 (в этой среде 3.11.2 — step «Python 3.13» упал бы первым); ruff/black не установлены как зависимости проекта |

Заключение: **Quality Gate на текущем main не может PASS ни в одном окружении**,
пока не устранены H-1: (1) 97 ruff-нахождений, (2) black-формат 67 файлов,
(3) декларация tooling-зависимостей. Запреты задания соблюдены: правила не
отключались, полный Ruff не заменялся более слабым набором, PASS не
декларировался при красном Ruff; dependency policy **не изменена** — G-03
представлен как рекомендация на одобрение.

Примечание по среде: аудит выполнялся на Python 3.11.2 в изолированном venv
(после аудита venv удалён, рабочее дерево чистое: `git status` — no changes).
Код не использует 3.13-специфичный синтаксис (полная зелёная на 3.11), но
требование `requires-python >= 3.13` сохранено как есть.

---

## Приложение. Методологические заметки

1. «Наличие файла ≠ доказательство»: deployment artifact, migrations,
   contract tests оценивались по содержимому и поведенческим тестам, а не по
   наличию путей.
2. Import-анализ проводился полным AST-сканом `src/` (включая function-level
   импорты) + runtime-проверками в чистых интерпретаторах (как это делают сами
   тесты проекта) + обходом графа живых объектов (как `test_component_boundary.py`).
3. Версионные декларации всех 9 межкомпонентных зависимостей сверены
   program-но с `component_version` провайдеров.
4. `UNPROVEN` использован только там, где правило не имеет объекта проверки в
   текущем режиме (§4.1) или проверка не существует; ни один UNPROVEN не
   переведён в CONFORMING.
5. Аудит не вносил изменений в репозиторий: `git status` — clean;
   все изменения, если будут применены, — по списку G (Level A).

---

## Приложение Б. Log исполнения G-списка (2026-09-10)

Решения владельца от 2026-09-10: G-03 — APPROVED; ADR-0011 — RATIFY
(исторический факт «PROPOSED при смерженной реализации» сохраняется и
задокументирован); G-01…G-12 — исполнены. Все изменения Level A (и
завершение ADR-процесса G-12); LAW-01…LAW-16, LAW-16a, Component
Definition, Data Ownership, Contract Model, Versioning, Migration Model,
Manifest, Golden Bundle, Fork Policy, Maturity Model и Tenant Isolation
не изменялись. Base: `8445e2b` (GitHub main на момент начала работы —
проверено: без дрейфа).

| G-item | Статус | Commit | Результат |
|---|---|---|---|
| G-01 (ruff 97) | DONE | `c811e0e` | 97 → 0: 51 исправлено в коде честно (B023 default-arg binding, F401, TRY004 ValueError→TypeError в type-check'ах, B017 сужение до FrozenInstanceError/AccessRefused, PLW1510, PLR1711/RET501, SIM114); 46 — осознанный fail-closed паттерн, сохранён и задокументирован per-file в `pyproject.toml` (не отключение правил) |
| G-02 (black 67) | DONE | `c811e0e` | 67 файлов отформатировано (обвязка строк, AST-safe); `black --check` → 0 |
| G-03 (tooling) | DONE | `d50bb4a` | dev extras: `ruff==0.16.6`, `black==26.5.1` (pinned); `[tool.ruff]` (default rule set целиком, line-length 88, `extend-immutable-calls` для FastAPI Query, задокументированные per-file-exceptions fail-closed); `[tool.black]` (line-length 88) |
| G-04 (CI) | DONE (workspace), применение в репозиторий — заблокировано платформой | — | `.github/workflows/quality-gate.yml` подготовлен (Python 3.13, compileall + ruff + black + pytest — те же шаги, что `quality-gate.ps1` + machine-verifiable dependency check `pip check`/`pip-audit --no-deps`; версии инструментов — закреплённые из pyproject). Push файла невозможен из sandbox: GitHub App не имеет права `workflows` (отклонение и по git push, и по API: «Resource not accessible by integration», HTTP 403). Файл лежит в workspace (`.github/workflows/quality-gate.yml`) и включён в итоговый отчёт; применение — один коммит владельцем либо выдача права `workflows` приложению |
| G-05 (saga range) | DONE | `3378cdb` | identity range `>=0.1.0,<0.2.0` → `>=0.3.0,<0.4.0`; все 9 range'ов повторно проверены machine-wise: 9/9 корректны |
| G-06 (baseline) | DONE | `e7b9db1` | фактические строки приведены к репозиторию (7 компонентов, 7/7 контрактов, 730 тестов, gate, CI); история сохранена (включая note об отклонении последовательности ADR-0011); зафиксировано NOT PUBLISHABLE по §30 как состояние foundation-режима |
| G-07 (README) | DONE | `e7b9db1` | «Статус» называет реализованные компоненты, ADR-0010/0011; standalone-режим и NOT PUBLISHABLE — сохранены |
| G-08 (ADR catalog) | DONE | `ed26871` | каталог: ADR-0010 RATIFIED 2026-09-08, ADR-0011 RATIFIED 2026-09-10 (+ историческая пометка) |
| G-09 (identity OpenAPI) | DONE | `3378cdb` | `info.version` 0.1.0 → 0.3.0; 7/7 openapi-версий соответствуют контрактам |
| G-10 (idempotency README) | DONE | `2364d28` | `components/idempotency/README.md` добавлен (I-001…I-008, consumer surface, replay/conflict/retry, non-goals, тесты) |
| G-11 (demo app) | DONE | `92fbcf6` | module-level demo-деплоймент удалён (ничего его не импортировало, не был задокументирован как вход, был единственным module-level объектом среди 4 api-модулей; импорт модуля больше без side effect) |
| G-12 (ADR-0011) | DONE | `ed26871` | ADR-0011 RATIFIED владельцем 2026-09-10 по конвенции ADR-0010; раздел Ratification с зафиксированным (не переписанным) процессным отклонением; ARCHITECTURE.md 1.2.0 не изменён, bump версии не требуется |
| NAM-01 (naming) | DONE | `3378cdb` | `component_id` `idempotency_guard` → `idempotency` (директория/пакет/остальные 6 компонентов — единый паттерн); обновлены saga/learning контракты, learning README, 2 contract-теста |

Итоговое состояние после G-списка: Quality Gate GREEN
(`compileall` ✓, `ruff check .` = 0, `black --check` = 0, `pytest` = 730
passed); ADR-каталог синхронизирован; документация отражает фактический
репозиторий; компоненты — foundation/standalone, NOT PUBLISHABLE по §30
(задокументировано, не нарушение). Единственное ожидающее действие
владельца — применить файл CI (G-04), который sandbox не вправе
запушить (нет права `workflows`).
