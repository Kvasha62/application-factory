# DOCUMENTATION_BASELINE.md

**Статус:** `CLEAN`  
**Дата:** 2026-09-15  
**Базовый `main` до ратификации:** `da3ac2e03255e6bfe0b42e0923326e9ed2bfb38d`  
**Актуализация:** 2026-09-14 — после прохождения Factory Gate #1 и ратификации ADR-0015 зафиксирована активация Level 2 — Component Factory; при этом сами фабричные механизмы остаются не реализованными до прохождения отдельных implementation slices. 2026-09-15 — после реализации Slice B — Component Catalog (Issue #64): каталог реализован как производный discoverable view реестра и опубликованных контрактов; Slice C/D/E остаются не реализованными до отдельных утверждённых work item.  
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

На момент этой актуализации **не считаются реализованными и доступными**:

- Platform Manifest implementation (Slice C);
- Golden Bundles (Slice D);
- Composer (Slice E);
- Platform Instance assembly tooling.

Slice A смержен (PR #63). Slice B прошёл Arena-верификацию и ожидает
независимого review и owner approval.
Последующие slices требуют отдельных work item, проверки, независимого review
и owner approval до merge.

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

`README.md` и `docs/adr/README.md` должны отражать, что Level 2 активирован, но фабричные механизмы ещё не реализованы.

Исторические conformance/audit документы не переписываются только потому, что проект развился после даты их снимка; они остаются историческими артефактами своего состояния.

## 8. Следующее изменение

**Slice B — Component Catalog реализован** (Issue #64) и проходит
независимый review.

Следующим самостоятельным этапом является **Slice C — Platform Manifest**.

До начала реализации Slice C необходимо иметь отдельный утверждённый work item и сохранить границу:

```text
ADR-0015 ratified
        ↓
Slice A — Component Registry (IMPLEMENTED, PR #63)
        ↓
Slice B — Component Catalog (IMPLEMENTED, Issue #64)
        ↓
approved implementation work item for Slice C
        ↓
Slice C — Platform Manifest
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
Manifest, Golden Bundles и Composer `NOT YET IMPLEMENTED`.

**Factory Gate #1: PASSED.**

Документационный baseline отражает переход от foundation/standalone состояния к активированному Level 2 без ложного утверждения, что не реализованные фабричные механизмы — Platform Manifest, Golden Bundles и Composer — уже существуют.
