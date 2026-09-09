# Conformance Review — ARCHITECTURE.md 1.2.0 против фактического `main`

**Проверяемый commit:** `8445e2bb707df2b9a16ae1a76074d018c620fe43` (`origin/main`)
**Дата аудита:** 2026-09-09
**Закон:** `docs/ARCHITECTURE.md` версии 1.2.0 (RATIFIED, основание — ADR-0010)
**Метод:** проверка дерева на зафиксированном commit + точечные запросы к GitHub API
для процессуального состояния (PR #32 MERGED 2026-09-09T12:13:06Z; Issues #29 и #24 OPEN).
Локальный клон shallow — история коммитов глубже HEAD напрямую не инспектировалась;
факт отсутствия правок закона после ратификации проверен через GitHub API
(всего 3 коммита, затрагивающих `docs/ARCHITECTURE.md`: init, upload, ratify 1.2.0).

**Классификация:** CONFORMING / PARTIAL / NON-CONFORMING / UNPROVEN / DOCUMENTATION GAP.
`UNPROVEN` нигде не превращается в `NON-CONFORMING` автоматически.
Архитектурный закон не изменяется и не ослабляется данным документом.

---

## A. Executive Summary

```text
Architecture:   CONFORMING  (закон 1.2.0 цел, не менялся после ратификации, §35 соблюдён)
Implementation: PARTIAL     (сильные границы/изоляция/идемпотентность/саги; нет Manifest;
                             нет OIDC; нет retention/удаления данных; нет метрик/логов/трейсов)
Documentation:  PARTIAL     (контракты и README компонентов сильны; DOCUMENTATION_BASELINE
                             устарел; ADR-0011 PROPOSED при смерженной реализации;
                             мелкие дрейфы версий/формулировок)
Tests:          CONFORMING  (730 passed; архитектурные требования покрыты тестами-доказательствами;
                             2 точечных пробела: контракт idempotency без теста, openapi-версия без parity)
Quality Gate:   NON-CONFORMING (pytest PASS, compileall PASS; ruff FAIL 97; black FAIL 67 файлов;
                             гейт не подключён к CI, инструменты не объявлены, скрипт только .ps1)
ADR compliance: PARTIAL     (ADR-0010 RATIFIED и исполнен; ADR-0011 PROPOSED, но Issue #31
                             закрыт и код в main; Issue #29 OPEN)
```

Критических (CRITICAL) нарушений нет: утечек tenant, секретов в коде,
прямого доступа к чужим данным, тихих изменений закона — не обнаружено.

---

## B. Conformance Matrix

| ID | Архитектурное правило | Evidence | Status | Severity | Action |
| -- | --------------------- | -------- | ------ | -------- | ------ |
| LAW-01 | Business Boundary — границы по бизнес-возможностям | 7 компонентов = 7 capability (IS-001/002/003/004/005/006, SCS-001); `components/*/contract/component_contract.json` (`component_id/class/task`); `components/*/README.md` | CONFORMING | — | — |
| LAW-02 | Component Ownership — один владелец, самостоятельный lifecycle | Владелец репозитория: `.github/CODEOWNERS` (`* @Kvasha62`); независимые версии 0.1.0/0.2.0/0.3.0. НО: в контрактах нет поля `owner` и нет статуса lifecycle §19 (F-033 закрыт наполовину) | PARTIAL | LOW | CR-L03: добавить `owner` (+ `status` §19) в дескрипторы |
| LAW-03 | Data Ownership — компонент владеет своими данными | `src/*/store.py` — изолированные in-memory stores; `data_ownership` + scopes в 7/7 контрактах; AST-тесты импортов (`test_authorization_dependency_boundary.py`, `test_records_bypass_resistance.py`, `test_learning_boundary.py`, `test_tenant_context_single_source.py`) | CONFORMING | — | — |
| LAW-04 | No Direct Database Sharing — только опубликованные контракты | Потоки данных только через published clients/ports/adapters (`reader.py`+`transport.py`+`adapters.py`); сканы AST + fresh-interpreter тесты; `identity→TA` ограничен guard-тестом (`TENANT_AUTHORITY_PUBLISHED_MODULES`); `guard.py` — объявленный `consumer_surface` IS-005 | CONFORMING | — | Примечание CR-L04 (provider-side декларация) |
| LAW-05 | Contract First | 7/7 машиночитаемых контрактов с §6.1-минимумом; OpenAPI 7/7; parity-тесты declared==implemented (`test_*_contract.py` ×4, saga state-model, identity context) | CONFORMING | — | Пробелы: CR-L02 (idempotency без теста), CR-L01 (openapi-дрейф) |
| LAW-06 | Independent Evolution | Диапазоны `>=x,<y` в `dependencies` 8/9 (см. CR-L14); additive-тест `0.2.0 -> 0.3.0` (`test_identity_context_contract.py:154`); `compatibility_policy` 7/7 | CONFORMING | — | CR-L14: stale range saga→identity |
| LAW-07 | Version Everything | SemVer в коде+контрактах; request/correlation/tenant/saga/event/audit ids; один API major v1 везде; digest — N/A (артефактов нет) | PARTIAL | LOW | CR-L01 (identity openapi 0.1.0≠0.3.0), CR-L06 (idempotency без констант версии) |
| LAW-08 | Forward Migration | Постоянной схемы нет — мигрировать нечего (LAW-13 запрещает выдумывать миграции); CI-проверка N-3→N dormant («после появления CI», CI нет) | CONFORMING (vacuous) | — | — |
| LAW-09 | Reproducible Platform | Manifest отсутствует полностью: файлов нет, в `src/` ноль упоминаний «manifest» | NON-CONFORMING | HIGH | CR-H02: минимальный standalone-manifest + 1 attestation (§4.1/§16, Level A) |
| LAW-10 | Certified Composition | Bundle нет; гейты §4.1 не достигнуты (0 независимо поставляемых, 0 профилей, 0 внешних потребителей) — строить их сейчас нарушало бы LAW-13 | CONFORMING (vacuous) | — | — |
| LAW-11 | Configuration Before Fork | Форков нет; строгие конфиги (`config.py` ×5, unknown key = error, тесты `test_identity_configuration.py` и др.) | CONFORMING | — | — |
| LAW-12 | Microservice Is Not a Product Boundary | Модульный монолит; `transport.py` — in-process исполнение HTTP-контрактов (задекларировано как Level-0 API mode), не сервисы | CONFORMING | — | — |
| LAW-13 | Complexity Must Be Earned | Нет каталога/bundle/composer/trains/broker/CDC/событий; гейты проверены и не достигнуты | CONFORMING | — | — |
| LAW-14 | Architecture Changes Through ADR | ADR-0010 RATIFIED → 1.2.0; `docs/adr/` — единый каталог; закон не менялся после ратификации (§35 ✓). НО: ADR-0011 PROPOSED при смерженной реализации (PR #32), Issue #29 OPEN; стек (FastAPI/pytest/in-process) без документа уровня (M6) | PARTIAL | MEDIUM | CR-M01 (закрыть ревью ADR-0011), CR-M05 (зафиксировать стек) |
| LAW-15 | Reversibility | Порты/адаптеры сохраняют путь выделения; in-process транспорт обратим; консолидация не заблокирована | CONFORMING | — | — |
| LAW-16/16a | Tenant Isolation + Data Scope Classification | Effective tenant из verified identity (`IdentityEngine.resolve_tenant_context`, `source=verified_identity`); caller `tenant_id` — только cross-check (`X-Tenant-Id`→`claimed_tenant_id`→`TENANT_MISMATCH`, тесты во всех компонентах); scopes на каждом датасете 7/7; platform-scoped listing авторизован+аудирован (`test_registry_listing_is_scoped_to_the_current_platform`); single-source тесты (17 шт.) | CONFORMING | — | — |
| §2.3 | Tenant lifecycle | `TenantState` + `ALLOWED_TRANSITIONS` + `LIFECYCLE_CHAIN` (`src/tenant_authority/lifecycle.py:29-44`) = `provisioning→active→suspended→deletion_requested→deleted`; тесты `test_tenant_lifecycle.py` (17), `test_lifecycle_enforcement.py` (11) | CONFORMING | — | Примечание: есть ребро reactivation `suspended→active` сверх линейной цепочки (Level A, протестировано) |
| §5.4 | Multi-tenancy scopes | Scopes объявлены 7/7 (`tenant-scoped` по умолчанию для бизнеса; `platform-scoped` реестры/сервисы) | CONFORMING | — | — |
| §6.2 | SSO/Identity; authn≠authz; authz на границе владельца | authn: demo bearer + service identity (см. PARTIAL ниже); authz: `default DENY`, решения IS-003 + enforcement владельца (`test_every_published_route_refuses_without_an_allow`, `test_denied_write_never_reaches_the_state_machine_in_the_monolith`, `test_authentication_does_not_imply_authorization_behaviorally`) | PARTIAL | MEDIUM | CR-M04: OIDC не реализован (честно задекларировано `oidc_compatible_bearer_token`, «not a full IAM product») |
| §6.3/§8 | Events / CloudEvents / versioning | Событий нет: `events.status=declared_only`, `published=[]` 7/7, залочено тестами (`test_contract.py`, `test_*_contract.py`) | CONFORMING (vacuous) | — | — |
| §6.4 | Saga Contract (11 полей) | `saga_id/correlation_id/tenant_id+scope/step_id/state/retry_policy/timeout/compensation/idempotency/terminal_state` — всё в `src/saga/models.py` + `saga_contract` дескриптора; компенсация обязательна до шага (`StepDefinition.__post_init__`); терминальные `{COMPLETED,COMPENSATED,FAILED}`; timeout→failure (`FailureKind.TIMEOUT`); компенсация идемпотентна через IS-005 (`step_idempotency_key`); тесты A–H + state-model + idempotency + boundary + observability | CONFORMING | — | — |
| §6.5 | Idempotency: event_id / Idempotency-Key / command_id; replay≠retry | Класс 2: `IdempotencyGuard` + тесты (replay/conflict/binding/same-key concurrency, 11 шт.) + per-component тесты — CONFORMING. Класс 3: substance через step-ключи+guard, replay≠retry доказан (`test_scenario_c_*`, `test_scenario_h_*`) — PARTIAL (нет литерала `command_id`, 0 вхождений). Класс 1: событий нет — vacuous | PARTIAL | LOW | CR-L08: терминология `command_id` |
| §6.6/§7 | API: OpenAPI, /api/v1, versioning, ≤2 majors, Sunset | OpenAPI 7/7; все пути `/api/v1/...` (learning — server `/api/v1/learning` + относительные пути); `supported_majors=[v1]`; additive-политики; deprecation нет → Sunset N/A | CONFORMING | — | CR-L01 (identity openapi version drift) |
| §6.7/§6.8 | UI / Design Tokens | `ui: none/declared_only` 7/7, залочено тестами | CONFORMING (vacuous) | — | — |
| §6.9/§24 | Data Export / CDC / Analytics | `data_export_cdc.status=declared_only`, `streams=[]` 7/7; аналитики нет; кросс-tenant аналитики нет | CONFORMING (vacuous) | — | — |
| §9 | Schema Registry | Реестра нет; общих схем нет (кроме Python-типов TA, см. CR-L04). «Допускается пакет» — дозволение, не обязанность | UNPROVEN (dormant) | LOW | Не строить реестр до первых общих схем (LAW-13); закрыть CR-L04 |
| §10 | Contract Testing | Дескрипторные тесты 6/7 + parity declared==implemented (4 HTTP + saga + identity) + behavior-тесты границ | PARTIAL | LOW | CR-L02: тест дескриптора idempotency |
| §11 | Identifiers | Immutable/opaque/unique, reuse запрещён (`test_a_saga_id_is_stable_and_cannot_be_reused`); префиксный формат — временное решение (§11 прямо разрешает); `id+version+digest` для артефактов — N/A (артефактов нет) | CONFORMING | — | — |
| §12/§13 | Migrations / Rollback | Постоянной схемы и продакшена нет — dormant | CONFORMING (vacuous) | — | — |
| §14/§15/§17 | Bundle / Compat Matrix / Composer | Нет; гейты не достигнуты — строить запрещено (LAW-13) | CONFORMING (vacuous) | — | — |
| §16/§31 | Platform Manifest / Platform DoD | См. LAW-09 — manifest отсутствует | NON-CONFORMING | HIGH | CR-H02 (один finding на §16+§31+LAW-09) |
| §18 | Dependency Graph | 9 рёбер, все объявлены в `dependencies`, все — через published clients/ports/adapters или declared `consumer_surface`; запрещённых рёбер (DB/internal/private/queue) нет — доказано AST+fresh-interpreter тестами 4 потребителей + direction-тестом TA | CONFORMING | — | Граф — в Приложении D; CR-L04, CR-L14 |
| §19 | Component Lifecycle | §19-статусы компонентов нигде не объявлены (см. LAW-02) | PARTIAL | LOW | CR-L03 |
| §20 | Configuration | Декларативные версионированные конфиги; unknown key = `ConfigurationError` (код ×5 + `additionalProperties:false` в схемах 7/7); секретов в коде нет; overlays N/A (один standalone-env) | CONFORMING | — | — |
| §21/§22 | Extensions / Fork | Расширений нет; форков нет | CONFORMING | — | — |
| §23/§25/§28/§29 | Profiles / Scaling / Release / Upgrade | Дозволенные отсутствия (pre-factory, нет продакшена); §28 прямо разрешает отсутствие политики | CONFORMING | — | — |
| §26/AMD-10 | Observability | Context 6/6 (timestamp/environment/platform/component/version/request/trace/correlation/tenant/actor; `saga_id` осознанно отсутствует — саг между компонентами нет); health/ready 5/5 HTTP; audit-контексты; `test_*_observability.py`, `test_*_audit.py`. Метрик/structured-logs/трейсов нет (клауза «production-компонент» — продакшена нет, dormant) | PARTIAL | LOW | CR-L10: до первого prod-claim |
| §27/AMD-06/11 | Security + Audit | Секретов в коде нет (скан); least privilege (`LOOKUP_PERMISSIONS`, точные ассёрты); authn/authz/изоляция — см. выше; audit append-only (`test_audit_journal_is_append_only`, `test_audit_never_records_credentials`, per-component журналы). Retention/удаление данных: НЕТ (ноль артефактов). Проверка зависимостей: механизма нет | PARTIAL | MEDIUM | CR-M03 (retention/удаление), CR-L09 (dependency audit) |
| §30 | Component DoD | Таблица — Приложение B. Статус: все компоненты NOT PUBLISHABLE (нет артефактов/каталога) — консистентный статус pre-catalog стадии, не нарушение | PARTIAL (статус) | — | G (по готовности публикации) |
| §32/§33 | Dev / AI rules | Границы/ownership/совместимость соблюдены в коде; нет прямых доступов/форков/latest/удалённых миграций/микросервисов | CONFORMING | — | — |
| §34/§34.2 | ADR | ADR-0010 RATIFIED, реален, в `docs/adr/`; история достоверна (§35: 3 коммита). ADR-0011 — см. LAW-14 | PARTIAL | MEDIUM | CR-M01 |
| §35 | No silent change | Закон не менялся после ратификации (GitHub API) | CONFORMING | — | — |
| §40/§41 | Audit F-001…F-034 / Changelog | Реестр 34 шт. в ADR-0010 сверен построчно — полный; changelog 1.0.0/1.1.0/1.2.0 на месте | CONFORMING | — | — |
| §42 | Conformance baseline | Automated checks существуют почти на каждое машиночитаемое правило (матрица §9/Приложение C); manual review procedures не регистрировались (альтернатива не использована — допустимо) | PARTIAL
...[45591 chars truncated]