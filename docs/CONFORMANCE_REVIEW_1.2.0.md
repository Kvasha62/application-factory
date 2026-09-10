# CONFORMANCE REVIEW — ARCHITECTURE.md v1.2.0

**Объект аудита:** `Kvasha62/application-factory`, commit `75a9f12bd10a0fc9fd409430970e15fe7d65e830` (`main`)
**Дата аудита:** 2026-09-10
**Вид документа:** отчёт о фактическом соответствии; не нормативный документ и не новый архитектурный закон
**Канонический закон:** `docs/ARCHITECTURE.md` v1.2.0 (`RATIFIED`, основание — ADR-0010)

> **Статус этого ревью:** POST-RATIFICATION CONFORMANCE REVIEW.
> Цель — проверить фактическую реализацию после ратификации ADR-0011 и отделить
> исторические замечания предыдущего аудита от актуальных gaps.

## 0. Scope и метод

Проверены:

- `docs/ARCHITECTURE.md` v1.2.0;
- `docs/adr/ADR-0010-architecture-gap-review.md`;
- `docs/adr/ADR-0011-scs-001-learning-content-authoring-boundary.md`;
- `docs/adr/README.md`;
- `docs/DOCUMENTATION_BASELINE.md`;
- `docs/OPERATING_MODEL.md`;
- `README.md`;
- `pyproject.toml`;
- `.github/CODEOWNERS`;
- `.github/workflows/quality-gate.yml`;
- 7 Component Contracts и 7 OpenAPI-документов;
- реализация семи компонентов в `src/`;
- тестовый контур, включая boundary, tenant-isolation, contract,
  learning-authoring, saga и idempotency tests.

Предыдущий аудит от 2026-09-09 использовался как baseline findings, но его выводы
не принимаются автоматически: каждый finding перепроверен по состоянию `main`.

Классы результата: `CONFORMING`, `PARTIAL`, `UNPROVEN`, `DOCUMENTATION GAP`,
`NOT APPLICABLE`.

---

## 1. Executive Summary

| Область | Актуальный статус | Итог |
|---|---|---|
| Архитектурный закон | `CONFORMING` | Нарушений LAW-01…LAW-16/16a не выявлено |
| SCS-001 / Learning Authoring | `CONFORMING` | ADR-0011 теперь `RATIFIED`; реализация Slice 4 соответствует решению |
| Component Contracts | `CONFORMING` | 7/7 существуют и синхронизированы с заявленным контуром |
| Tenant isolation / ownership | `CONFORMING` | Проверяемые границы сохранены |
| Idempotency / atomicity | `CONFORMING` | Authoring, publish/archive и saga используют заявленные boundaries |
| Documentation baseline | `CONFORMING` | Baseline обновлён под фактическое состояние |
| ADR catalog | `CONFORMING` | ADR-0010 и ADR-0011 отражены как RATIFIED |
| Quality Gate | `CONFORMING` | Инструменты закреплены в `pyproject.toml`; CI workflow существует |
| Factory mode | `CONFORMING` | Catalog/Composer/Golden Bundles не введены преждевременно |
| Publishable status | `NOT PUBLISHABLE` | Допустимое состояние standalone/foundation mode, не архитектурное нарушение |

**Главный вывод:** предыдущие HIGH/MEDIUM findings, относящиеся к красному Quality
Gate, PROPOSED ADR-0011, устаревшему baseline, версии saga → identity и устаревшему
identity OpenAPI, устранены или закрыты соответствующими изменениями документации
и конфигурации. Исторический факт нарушения последовательности
«ратифицировать → реализовать» для Slice 4 не скрыт: ADR-0011 сохраняет его в
разделе `Ratification`.

---

## 2. Архитектурная конституция — LAW-01…LAW-16a

### LAW-01 — Business Boundary

Семь компонентов организованы вокруг бизнес-возможностей, а не случайных
технических классов. Learning расширен именно бизнес-цепочкой
`Course → Module → Lesson → Assignment`, определённой ADR-0011.

**Статус: CONFORMING.**

### LAW-02 — Component Ownership

Каждый компонент имеет владельца в CODEOWNERS и собственную публичную версию и
contract identity. Жизненный цикл компонента отделён от жизненного цикла
потребителей.

**Статус: CONFORMING.**

### LAW-03 — Data Ownership

В Level 0 данные физически хранятся in-memory, но logical ownership разделён.
Learning владеет learning records; Tenant Authority — tenant registry;
Authorization — grants; Saga — workflow state; Idempotency — свой guard state.

**Статус: CONFORMING.**

### LAW-04 — No Direct Database Sharing

Физической БД в текущем контуре нет. Межкомпонентные зависимости проходят через
опубликованные поверхности; boundary-тесты охраняют отсутствие доступа к
внутренним модулям и объектам других компонентов.

**Статус: CONFORMING.**

### LAW-05 — Contract First

Для всех семи компонентов существуют machine-readable Component Contracts и
OpenAPI-документы. Для HTTP-компонентов контрактные тесты сопоставляют публичную
поверхность и реализацию.

**Статус: CONFORMING.**

### LAW-06 — Independent Evolution

Версии компонентов независимы; потребители используют contract/port surfaces.
Версионные зависимости соответствуют фактически потребляемым версиям.

**Статус: CONFORMING.**

### LAW-07 — Version Everything That Matters

Версии компонентов, контрактов и OpenAPI синхронизированы. `identity` OpenAPI
указывает `0.3.0`, а `saga → identity` объявляет `>=0.3.0,<0.4.0`.

**Статус: CONFORMING.**

### LAW-08 — Forward Migration

В текущем Level 0 нет production database schema и, следовательно, нет объекта
для миграции. Production migrations появятся до publishable-режима и должны быть
forward-only.

**Статус: NOT APPLICABLE в текущем standalone/in-memory контуре.**

### LAW-09 — Reproducible Platform

Полный Platform Manifest/Composer пока не введён. ARCHITECTURE.md §4.1 прямо
разрешает standalone-режим до достижения фабричных гейтов.

**Статус: CONFORMING — механизм пока не требуется.**

### LAW-10 — Certified Composition

Golden Bundles отсутствуют, поскольку фабричный режим ещё не активирован.

**Статус: CONFORMING — механизм пока не требуется.**

### LAW-11 — Configuration Before Fork

Customer forks не обнаружены. Конфигурационные схемы строгие; неизвестные ключи
отклоняются.

**Статус: CONFORMING.**

### LAW-12 — Microservice Is Not a Product Boundary

Проект остаётся Level 0 modular monolith. Новые микросервисы для Slice 4 не
вводились.

**Статус: CONFORMING.**

### LAW-13 — Complexity Must Be Earned

Catalog, Composer, Golden Bundles, event infrastructure и внешние workflow
engines не введены преждевременно. Это соответствует §4.1 и explicit non-goals
ADR-0011.

**Статус: CONFORMING.**

### LAW-14 — Architecture Changes Through ADR

ARCHITECTURE.md 1.2.0 основан на ADR-0010. ADR-0011 теперь `RATIFIED` и отражён
в каталоге ADR. Историческое отклонение последовательности для уже смерженного
Slice 4 сохранено в ADR-0011 и не переписывается.

**Статус: CONFORMING с историческим process deviation, закрытым ратификацией.**

### LAW-15 — Reversibility

Решение ADR-0011 ограничено первым authoring slice и явно не включает revision,
bulk authoring, reordering, events, factory mechanics или microservices. Это
сохраняет возможность дальнейшей эволюции без преждевременной фиксации.

**Статус: CONFORMING.**

### LAW-16 — Tenant Isolation

Effective tenant остаётся производным от verified identity. Caller-supplied
`tenant_id` используется только как cross-check. Learning authoring/read paths
проверяют tenant ownership, authorization и publication state. Cross-tenant
доступ к tenant-scoped данным не разрешается.

**Статус: CONFORMING.**

### LAW-16a — Data Scope Classification

Datasets объявляют ровно одну область (`tenant-scoped`, `platform-scoped` или
`system-scoped`). Learning business data tenant-scoped; platform registry/audit
и аналогичные служебные данные не превращаются в общие business datasets.

**Статус: CONFORMING.**

---

## 3. SCS-001 — Post-Ratification Conformance

### 3.1 ADR-0011

ADR-0011 имеет статус `RATIFIED`, принят владельцем проекта 2026-09-10. Он не
изменяет ARCHITECTURE.md 1.2.0 и не требует architecture version bump.

Решение фиксирует минимальную цепочку:

`Course → Module → Lesson → Assignment → Submission → Teacher Review`

и первую authoring-срезку: Create Course; Create Module; Create Lesson;
Create Assignment; Publish Course; Archive Course.

**Статус: CONFORMING.**

### 3.2 Реализация

Реализация Slice 4 существует в `learning_service` и включает create-команды для
четырёх уровней и бизнес-команды publish/archive. Публичный контракт Learning
остаётся версионированным `0.2.0`.

Publication и archive сериализуют операции над одной Course hierarchy через
course-scoped critical section; тем самым закрывается ранее выявленный race,
который мог привести к `PUBLISHED Course + DRAFT descendant`.

Опубликованная hierarchy после publication immutable; unpublish и editing
published content отсутствуют, как предписывает ADR-0011.

**Статус: CONFORMING.**

### 3.3 Invariants

Проверяемый контур включает ownership и parent-type integrity; tenant consistency
всей hierarchy; immutable identifiers; unique names; запрет удаления под Course;
atomic publication; immutable published hierarchy; atomic archive; запрет
`PUBLISHED Course + DRAFT descendant`; archive не публикует новый контент;
exactly-once effect через IS-005.

**Статус: CONFORMING.**

### 3.4 Security boundary

Authoring следует цепочке:

`Identity → effective tenant → Authorization → Learning ownership boundary`.

Student reads допускаются только для verified identity, правильного tenant,
авторизованного доступа и опубликованного контента.

**Статус: CONFORMING.**

### 3.5 Idempotency и concurrency

State-changing authoring commands используют IS-005. Publication/archive не
должны частично применяться при replay, binding conflict, authorization denial
или dependency failure. Course-scoped `RLock` сериализует конкурирующие mutation
операции внутри Level 0 процесса.

Это именно Level 0 гарантия; distributed lock или cross-process recovery ADR-0011
не вводит.

**Статус: CONFORMING в заявленном Level 0 scope.**

### 3.6 Scope discipline

Не обнаружено добавления функциональности, которую ADR-0011 объявляет non-goal:
grading, score, feedback, progress, analytics, media, payment, events/CDC solely
for authoring, factory mechanics, generic CRUD, revision, bulk authoring,
search/pagination или microservice decomposition.

**Статус: CONFORMING.**

---

## 4. Component Contract ↔ OpenAPI ↔ Code

| Component | Version | Contract | OpenAPI | Boundary | Status |
|---|---:|---|---|---|---|
| authorization | 0.1.0 | present | present | guarded | CONFORMING |
| identity | 0.3.0 | present | `info.version: 0.3.0` | guarded | CONFORMING |
| tenant_authority | 0.1.0 | present | present | guarded | CONFORMING |
| records | 0.1.0 | present | present | guarded | CONFORMING |
| learning | 0.2.0 | present | present | guarded | CONFORMING |
| saga | 0.1.0 | present | schema-only | guarded | CONFORMING |
| idempotency_guard | 0.1.0 | present | schema-only | guarded | CONFORMING |

All seven contracts contain the required architectural fields:
`api`, `events`, `data_export_cdc`, `ui`, `configuration_schema`, `data_ownership`,
`authn`, `authz`, `compatibility_policy`, `dependencies`.

`events` and `data_export_cdc` remain `declared_only` where no implementation exists;
this is deliberate non-claiming, not a missing implementation disguised as a
public capability.

**Статус: CONFORMING.**

---

## 5. Quality Gate и delivery verification

Quality Gate имеет единый источник инструментальных версий в `pyproject.toml`:

- Python `>=3.13`;
- `ruff==0.16.6`;
- `black==26.5.1`;
- `pytest>=8.3.0`;
- CI дополнительно запускает `pip-audit==2.10.1` и `pip check`.

`.github/workflows/quality-gate.yml` запускается на `push` в `main` и на pull
request и выполняет install → dependency check → compile → Ruff → Black → tests.

Таким образом предыдущие findings H-1/H-3/M-2/M-5, связанные с отсутствием
воспроизводимого tooling/CI-контуров, более не описывают текущее состояние.

**Статус: CONFORMING.**

> Branch protection и repository rulesets не считаются подтверждёнными этим
> ревью: доступ текущего GitHub integration к administration endpoints ограничен.
> Это ограничение аудита, а не утверждение об отсутствии protection/rulesets.

---

## 6. Component Definition of Done

Текущий проект остаётся `NOT PUBLISHABLE` по полному §30 DoD, поскольку
standalone/foundation режим ещё не содержит production migrations, реальных
deployable artifacts с фиксируемым digest и полной factory delivery machinery.
Event infrastructure также не реализована там, где она пока явно не заявлена.

Это **не нарушение** ARCHITECTURE.md: §4.1 разрешает standalone-режим до
достижения фабричных гейтов, а §30 описывает условия publishable component.

Фабричный режим не следует включать только потому, что в репозитории уже семь
логических компонентов: gate считает именно независимо поставляемые компоненты,
вертикальные профили или внешних потребителей.

**Статус: CONFORMING — current mode; NOT PUBLISHABLE — release capability.**

---

## 7. Findings reconciliation

| Previous finding | Previous status | Current status | Explanation |
|---|---|---|---|
| H-1 QG red/tooling unpinned | HIGH | CLOSED | Ruff/Black pinned and configured; CI workflow active |
| H-2 ADR-0011 PROPOSED | HIGH | CLOSED | ADR-0011 ratified 2026-09-10; historical deviation preserved |
| H-3 DOCUMENTATION_BASELINE stale | HIGH | CLOSED | Baseline updated to current repository facts |
| CTR-01 saga → identity range | MEDIUM | CLOSED | range is `>=0.3.0,<0.4.0` |
| CI-01 no CI | MEDIUM | CLOSED | `.github/workflows/quality-gate.yml` active |
| DOD-01 migrations absent | MEDIUM | OPEN / ACCEPTED | no physical DB in Level 0; blocks publishability only |
| DOD-02 deployable artifact absent | MEDIUM | OPEN / ACCEPTED | no production publication claimed |
| SEC-01 dependency checking unproven | MEDIUM | CLOSED | CI runs `pip-audit` and `pip check` |
| DOC-02 identity OpenAPI version | LOW | CLOSED | OpenAPI is `0.3.0` |
| DOC-03 ADR catalog stale | LOW | CLOSED | ADR-0011 listed as RATIFIED |
| DOC-04 README incomplete status | LOW | CLOSED | repository status aligned with current mode |
| DOC-05 idempotency README | LOW | OPEN / NON-BLOCKING | naming/documentation consistency remains to be decided separately |
| NAM-01 component identity mismatch | LOW | OPEN / NON-BLOCKING | `idempotency` package vs `idempotency_guard` component id; no boundary violation proven |
| SYM-01 module-level demo app | LOW | ACCEPTED | explicitly demo-only; no architecture violation |
| OBS-01 multiple idempotency implementations | LOW | ACCEPTED | separate owned state; no second mechanism inside saga/learning |
| LINT | LOW | CLOSED | previous gate tooling/configuration work addressed lint baseline |

No new HIGH or CRITICAL architectural finding was identified.

---

## 8. Open items

### O-01 — Publishability prerequisites

When the project deliberately moves beyond standalone/foundation mode, introduce
migrations, deployable artifacts, and any required event infrastructure through
an appropriately scoped implementation/architecture decision. Do not introduce
factory machinery before §4.1 gate criteria are met.

**Severity: MEDIUM, future-release blocker only.**

### O-02 — Idempotency component naming/documentation consistency

Decide later whether the public `component_id` should be `idempotency` or the
package/directory should be renamed to `idempotency_guard`, and whether a dedicated
README is useful. This is a naming/documentation issue, not a data-boundary or
security defect.

**Severity: LOW.**

### O-03 — Repository protection verification

Manually verify branch protection/rulesets in GitHub repository settings when
administration visibility is available. Do not infer absence from API access
failure.

**Severity: LOW / audit limitation.**

---

## 9. Final verdict

```text
ARCHITECTURE.md 1.2.0:      CONFORMING
ADR-0011:                   RATIFIED / CONFORMING
SCS-001 Slice 4:            CONFORMING
Tenant isolation:           CONFORMING
Ownership boundaries:       CONFORMING
Contracts:                  CONFORMING
Idempotency/atomicity:      CONFORMING
Quality Gate:               CONFORMING
Documentation baseline:     CONFORMING
Factory gate discipline:    CONFORMING
Publishability:             NOT PUBLISHABLE (by design, current mode)
Critical findings:          NONE
High findings:              NONE
```

**Решение:** текущая реализация соответствует ратифицированной
`ARCHITECTURE.md` v1.2.0 и ADR-0011 в заявленном Level 0 standalone scope.
Дополнительный ADR для этого conformance review не требуется: архитектурный
закон не изменён.

Следующий архитектурный шаг не должен быть «доделать Slice 4» — он уже
соответствует ратифицированному решению. Следующий шаг выбирается отдельно:
новая бизнес-возможность SCS-001, иной компонентный slice или переход к
фабричному режиму только при фактическом достижении §4.1 gate.
