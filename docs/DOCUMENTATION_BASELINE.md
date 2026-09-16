# DOCUMENTATION_BASELINE.md

**Статус:** `CLEAN`  
**Дата:** 2026-09-16  
**Базовый `main` до ратификации:** `da3ac2e03255e6bfe0b42e0923326e9ed2bfb38d`  
**Актуализация:** 2026-09-14 — после прохождения Factory Gate #1 и ратификации ADR-0015 зафиксирована активация Level 2 — Component Factory; при этом сами фабричные механизмы остаются не реализованными до прохождения отдельных implementation slices. 2026-09-15 — после реализации Slice B — Component Catalog (Issue #64): каталог реализован как производный discoverable view реестра и опубликованных контрактов; после реализации Slice C — Platform Manifest (Issue #66, PR #67): манифест реализован как явный машинно-читаемый артефакт композиции платформы; после реализации Slice D — Golden Bundles (Issue #71): Golden Bundle реализован как явный машинно-читаемый артефакт сертифицированного воспроизводимого набора совместимых версий компонентов; Slice E — Composer реализован (Issue #73, merge `319ab4b`): детерминированная композиция и детерминированное отклонение несовместимых или contract-invalid сборок платформы. 2026-09-16 — после реализации Slice F — Platform Instance Assembly (Issue #74): assembly-представление конкретного Platform Instance реализовано как детерминированная привязка `platform_id` к принятому/валидному Platform Manifest; deployment/provisioning/rollout-исполнение остаётся не реализованным и требует отдельного work item.
**Основание:** `docs/ARCHITECTURE.md` 1.2.0 + `docs/adr/ADR-0010-architecture-gap-review.md` + `docs/adr/ADR-0015-level-2-component-factory-activation.md`

## 1. Назначение

Этот документ фиксирует подтверждённое состояние документационного фундамента и реализации проекта.

`CLEAN` означает не «документация полна», а следующее:

- единственный действующий архитектурный закон определён;
- нормативные ADR имеют единый каталог;
- ложные или неподтверждённые нормативные ссылки не используются;
- отсутствующие исторические артефакты не выдаются за существующие;
- операционный контур зафиксирован каноническим документом без создания второго архитектурного источника истины;
- документация различает архитектурную активацию Level 2 и фактическое наличие фабричных механизмов;
- текущее состояние проекта может быть продолжено без реконструкции несуществующей архитектурной истории.

## 2. Канонические источники

| Область | Источник | Статус |
|---|---|---|
| Архитектурный закон | `docs/ARCHITECTURE.md` | RATIFIED 1.2.0 |
| Архитектурные решения | `docs/adr/` | ACTIVE; ADR-0010…ADR-0015 подтверждены, ADR-0015 RATIFIED 2026-09-14 |
| Активация Component Factory | `docs/adr/ADR-0015-level-2-component-factory-activation.md` | RATIFIED; Level 2 ACTIVE |
| Документационный baseline | `docs/DOCUMENTATION_BASELINE.md` | CLEAN |
| Операционная модель | `docs/OPERATING_MODEL.md` | RATIFIED 1.0.0 |
| Git / PR / CI процесс | `docs/GIT_OPERATING_PROTOCOL.md` | ACTIVE 1.1.4 |
| Проектная ориентация | `README.md` | ALIGNED |
| Реализация компонентов | `src/` | существует: 9 компонентов; Level 0 modular monolith |
| Component Contracts | `components/*/contract/` | существуют для 9 компонентов |
| Component Registry | `factory/registry/component_registry.json` | Slice A IMPLEMENTED; canonical machine-readable реестр, 9 компонентов |
| Component Catalog | `factory/catalog/component_catalog.json` | Slice B IMPLEMENTED; производный discoverable view реестра, 9 компонентов |
| Platform Manifest | `factory/platform_manifest/schema/platform_manifest.schema.json` + `src/platform_manifest/` | Slice C IMPLEMENTED; явный машинно-читаемый артефакт композиции платформы |
| Golden Bundle | `factory/golden_bundle/schema/golden_bundle.schema.json` + `src/golden_bundle/` | Slice D IMPLEMENTED; явный машинно-читаемый артефакт сертифицированного воспроизводимого набора совместимых версий компонентов |
| Composer | `factory/composer/schema/composition_request.schema.json` + `src/composer/` | Slice E IMPLEMENTED; детерминированная композиция платформы и отклонение несовместимых или contract-invalid сборок |
| Platform Instance | `factory/platform_instance/schema/platform_instance.schema.json` + `src/platform_instance/` | Slice F IMPLEMENTED; детерминированная assembly-репрезентация конкретного Platform Instance из принятого/валидного Platform Manifest |
| Contract / boundary tests | `tests/` | существуют; автоматическая проверка архитектурных границ активна |
| Quality Gate | `scripts/quality-gate.ps1` + `.github/workflows/quality-gate.yml` | GREEN на ранее подтверждённом main; для текущей ратификационной ветки требуется CI-проверка PR |
| Маршрутизация ответственности | `.github/CODEOWNERS` | существует |

`docs/OPERATING_MODEL.md` определяет, как команда работает через GitHub, и не создаёт архитектурных законов. При конфликте приоритет у `docs/ARCHITECTURE.md`.

## 3. Подтверждённые архитектурные решения

- `ADR-0010` — архитектурный gap review и ратификация `ARCHITECTURE.md` 1.2.0 — `RATIFIED`.
- `ADR-0011` — SCS-001 Learning Content Authoring Boundary — `RATIFIED`.
- `ADR-0012` — SCS-001 Student Enrollment Boundary — `RATIFIED`.
- `ADR-0013` — Commerce Product Boundary / SCS-002 — `RATIFIED`.
- `ADR-0014` — SCS-003 Booking Boundary — `RATIFIED`.
- `ADR-0015` — Level 2 Component Factory activation after Factory Gate #1 — `RATIFIED` 2026-09-14.

`ADR-0009` отсутствует в подтверждённой истории и не реконструируется предположением.

## 4. Состояние реализации

В репозитории подтверждены 9 компонентов Level 0:

- `authorization`;
- `identity`;
- `tenant_authority`;
- `records`;
- `learning` — SCS-001;
- `saga`;
- `idempotency`;
- `commerce` — SCS-002;
- `booking` — SCS-003.

Три независимых Business Systems подтверждают Factory Gate #1:

- SCS-001 Learning;
- SCS-002 Commerce;
- SCS-003 Booking.

## 5. Состояние фабрики

**Архитектурный уровень:** `Level 2 — ACTIVE`.

Активация произведена ADR-0015 и означает разрешение на постепенное введение фабричных механизмов. Она не означает, что эти механизмы уже реализованы.

**Slice A — Component Registry: `IMPLEMENTED`.**

Канонический machine-readable реестр реализован:

```text
factory/registry/component_registry.json            canonical registry
factory/registry/schema/component_registry.schema.json  normative schema
factory/registry/README.md                          формат и правила реестра
src/component_registry/                             загрузка и валидация
tests/test_component_registry.py                    focused tests
```

Реестр содержит factory-level metadata 9 подтверждённых компонентов и не
владеет бизнес-данными компонентов (ADR-0015 §4, §11).

**Slice B — Component Catalog: `IMPLEMENTED`** (Issue #64).

Производный discoverable catalog реализован:

```text
factory/catalog/component_catalog.json                 canonical derived catalog
factory/catalog/schema/component_catalog.schema.json   normative schema
factory/catalog/README.md                              формат и правила каталога
src/component_catalog/                                 derivation, валидация, discovery
tests/test_component_catalog.py                        focused tests
```

Каталог выводится из канонического реестра и опубликованных контрактов,
закреплён за реестром content digest каждой записи и не является вторым
источником истины: любое расхождение с canonical registry/contract metadata
отвергается валидацией (ADR-0015 §5).

**Slice C — Platform Manifest: `IMPLEMENTED`** (Issue #66, PR #67).

Явный машинно-читаемый артефакт композиции платформы реализован:

```text
factory/platform_manifest/schema/platform_manifest.schema.json  normative schema
factory/platform_manifest/README.md                             формат и правила манифеста
src/platform_manifest/                                          load, build, validation, lifecycle
tests/test_platform_manifest.py                                 focused tests
```

Манифест фиксирует конкретную композицию платформы (явные версии компонентов,
artifact identity/digests, identity/version модель, lifecycle, approval/publication)
и ссылается на authoritative metadata из Registry и Catalog, не создавая второй
источник истины. Опциональная ссылка на Golden Bundle допустима, но сам
Golden Bundle (Slice D) этим slice не реализуется.

**Slice D — Golden Bundles: `IMPLEMENTED`** (Issue #71).

Явный машинно-читаемый артефакт сертифицированного воспроизводимого набора
совместимых версий компонентов реализован:

```text
factory/golden_bundle/schema/golden_bundle.schema.json  normative schema
factory/golden_bundle/README.md                          формат и правила bundle
factory/golden_bundle/example_bundle.json                пример полного инвентаря реестра
src/golden_bundle/                                       load, build, lifecycle, validation
tests/test_golden_bundle.py                              focused tests
```

Golden Bundle фиксирует явные версии компонентов и artifact identity,
идентифицируется `bundle_id + version + digest`, имеет явный lifecycle
`draft → candidate → certified → deprecated → revoked`, фиксирует проверенные
попарные отношения совместимости закреплённого набора и проверяет их против
authoritative Component Registry. Он не решает и не подставляет зависимости,
не собирает платформу и не становится вторым источником истины (ADR-0015 §7).

**Slice E — Composer: `IMPLEMENTED`** (Issue #73).

Детерминированная композиция платформы реализована:

```text
factory/composer/schema/composition_request.schema.json  normative schema
factory/composer/README.md                                формат и правила Composer
factory/composer/example_request.json                     детерминированный пример
src/composer/                                             resolution, проверки, сборка Manifest
tests/test_composer.py                                    focused tests
```

Composer разрешает объявленные зависимости, выбирает версии детерминированно,
проверяет совместимость, contract validity, configuration (§20) и extensions
(§21), собирает draft Platform Manifest и передаёт его на validation Slice C.
Composer не утверждает, не публикует и не разворачивает платформу, не вводит
собственный индекс версий и не становится вторым источником истины
(ADR-0015 §8, §11; ARCHITECTURE.md §17).

**Slice F — Platform Instance Assembly: `IMPLEMENTED`** (Issue #74).

Детерминированная assembly конкретного Platform Instance из принятого/
валидного Platform Manifest реализована:

```text
factory/platform_instance/schema/platform_instance.schema.json  normative schema
factory/platform_instance/README.md                             формат и правила
factory/platform_instance/example_instance.json                 детерминированный пример
src/platform_instance/                                          assembly, validation, digest
tests/test_platform_instance.py                                 focused tests
```

Assembly привязывает `platform_id` точно к identity/version/digest исходного
манифеста, наследует конкретные версии компонентов, artifact identities,
configuration, extensions, branding и ссылку на Golden Bundle (либо явный
статус uncertified, §31); environment-specific settings входят через
существующий контрактный путь configuration (§20); результат несёт
детерминированный `instance_digest` без зависимости от времени, случайности,
окружения и сети. Assembly перепроверяет composition поверхностями
существующих factory-контрактов (§18, §20, §21, contract validity) и не
создаёт второго источника истины. Это representation step: deployment,
provisioning и rollout не реализованы (ADR-0015 §15; ARCHITECTURE.md §2.2,
§31).

На момент этой актуализации **не считаются реализованными и доступными**:

- deployment/provisioning/rollout-исполнение поверх собранного Platform
  Instance.

Slice A смержен (PR #63). Slice B смержен (PR #65). Slice C смержен (PR #67).
Slice D смержен (PR #72, merge `d199f7a`). Slice E смержен
(`319ab4bed5a668d215fe4aa92badf86417722926`). Slice F находится в отдельном
PR и ожидает owner approval до merge. Последующие этапы требуют отдельных
work item, проверки, независимого review и owner approval до merge.

## 6. Границы Level 2

Активация фабрики не изменяет:

- границы Learning, Commerce или Booking;
- владение бизнес-данными;
- опубликованные контракты;
- правила совместимости и версионирования;
- запрет на `latest` как архитектурную зависимость;
- запрет на прямой доступ фабрики к внутренним БД компонентов;
- запрет на прямые импорты внутренних модулей компонентов как механизм композиции;
- правило отдельного ADR для новых архитектурных границ.

Фабрика владеет только factory-level metadata и knowledge of composition: registry metadata, catalog metadata, bundles, manifests и compatibility metadata.

## 7. Документационная целостность

`README.md` и `docs/adr/README.md` должны отражать, что Level 2 активирован, а фабричные механизмы вводятся инкрементально: Slice A/B/C/D/E/F реализованы, deployment/provisioning/rollout-исполнение поверх собранного Platform Instance — нет.

Исторические conformance/audit документы не переписываются только потому, что проект развился после даты их снимка; они остаются историческими артефактами своего состояния.

## 8. Следующее изменение

**Slice F — Platform Instance Assembly: `IMPLEMENTED`** (Issue #74; PR открыт;
ожидается owner approval и merge).

Следующим самостоятельным этапом является **deployment/provisioning/rollout-
исполнение поверх собранного Platform Instance**, но только после отдельного
утверждённого work item и после завершения governance-процесса Slice F.

До начала следующего этапа необходимо сохранить границу:

```text
ADR-0015 ratified
        ↓
Slice A — Component Registry (IMPLEMENTED, PR #63)
        ↓
Slice B — Component Catalog (IMPLEMENTED, Issue #64, PR #65)
        ↓
Slice C — Platform Manifest (IMPLEMENTED, Issue #66, PR #67)
        ↓
Slice D — Golden Bundles (IMPLEMENTED, Issue #71, PR #72, merge d199f7a)
        ↓
Slice E — Composer (IMPLEMENTED, Issue #73, merge 319ab4b)
        ↓
Slice F — Platform Instance Assembly (IMPLEMENTED in this PR; awaiting owner approval/merge)
        ↓
approved implementation work item for the next stage
        ↓
deployment/provisioning/rollout-исполнение поверх собранного Platform Instance
        ↓
verification / independent review
        ↓
owner approval
        ↓
merge
```

## 9. Итог

**DOCUMENTATION BASELINE: CLEAN.**

**Architecture Level 2: ACTIVE.**

**Factory mechanisms:** Slice A — Component Registry `IMPLEMENTED`;
Slice B — Component Catalog `IMPLEMENTED`;
Slice C — Platform Manifest `IMPLEMENTED`;
Slice D — Golden Bundles `IMPLEMENTED`;
Slice E — Composer `IMPLEMENTED`;
Slice F — Platform Instance Assembly `IMPLEMENTED in this PR; pending merge`;
deployment/provisioning/rollout-исполнение `NOT YET IMPLEMENTED`.

**Factory Gate #1: PASSED.**

Документационный baseline отражает переход от foundation/standalone состояния к активированному Level 2. Slice A–E зафиксированы как `IMPLEMENTED` после merge; Slice F — Platform Instance Assembly отражён как реализованный в текущем PR и ожидающий merge; deployment/provisioning/rollout-исполнение явно помечено как `NOT YET IMPLEMENTED`.
