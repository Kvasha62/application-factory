# DOCUMENTATION_BASELINE.md

**Статус:** `CLEAN`
**Дата:** 2026-09-11
**Ветка создания:** `chore/phase-0-repository-cleanup` (ветка впоследствии удалена)
**Исторический commit:** `19c8a5b1efe31d01b8b7044e197b11c44f7fa2f4`
**Актуализация:** 2026-09-08 — Issue #6 (DOC-002): ратифицирован и включён в контур `docs/OPERATING_MODEL.md` как канонический операционный источник
**Актуализация:** 2026-09-10 — состояние приведено в соответствие с фактическим репозиторием (реализация компонентов, Component Contracts, contract-test suite, Quality Gate, CI). История перехода к реализованному состоянию сохраняется, в том числе зафиксированное в ADR-0011 (раздел Ratification) отклонение последовательности «ратифицировать → реализовать» для среза 4 SCS-001
**Актуализация:** 2026-09-11 — Issue #49 (Foundation Health Review): фактические показатели приведены к текущему `main` (`de13ab39c65ffe83e11c0f15b0f90ccacdeb5d2a`), который успешно прошёл Quality Gate (GitHub Actions run `34582747555`, 2026-09-11); количество тестов уточнено по фактическому прогону suite; отражено появление ADR-0012 и состояние SCS-001 после Student Enrollment. Проверены как актуальные: `docs/ARCHITECTURE.md` 1.2.0, `docs/OPERATING_MODEL.md` 1.0.0, `docs/GIT_OPERATING_PROTOCOL.md` 1.1.4, ADR-0010/0011/0012. Архитектура, ADR, Component Contracts и код не изменялись. Branch protection/rulesets этим документом не подтверждаются.
**Основание:** `docs/ARCHITECTURE.md` 1.2.0 + `docs/adr/ADR-0010-architecture-gap-review.md`

## 1. Назначение

Этот документ фиксирует состояние документационного фундамента после ратификации ARCHITECTURE.md 1.2.0.

`CLEAN` означает не «документация полна», а следующее:

- единственный действующий архитектурный закон определён;
- нормативные ADR имеют единый каталог;
- ложные или неподтверждённые нормативные ссылки удалены;
- отсутствующие исторические артефакты не выдаются за существующие;
- операционный контур зафиксирован каноническим документом без создания второго архитектурного источника истины;
- состояние документации соответствует текущему standalone-режиму;
- следующий этап может начинать реализацию без реконструкции несуществующей архитектурной истории.

## 2. Канонические источники

| Область | Источник | Статус |
|---|---|---|
| Архитектурный закон | `docs/ARCHITECTURE.md` | RATIFIED 1.2.0 |
| Архитектурные решения | `docs/adr/` | ACTIVE (ADR-0010 RATIFIED 2026-09-08; ADR-0011 RATIFIED 2026-09-10; ADR-0012 RATIFIED 2026-09-10) |
| Текущее ADR конституционного изменения | `docs/adr/ADR-0010-architecture-gap-review.md` | RATIFIED |
| Документационный baseline | `docs/DOCUMENTATION_BASELINE.md` | CLEAN |
| Операционная модель | `docs/OPERATING_MODEL.md` | RATIFIED 1.0.0 |
| Проектная ориентация | `README.md` | ALIGNED |
| Реализация компонентов | код соответствующего компонента (`src/`) | существует: 7 компонентов, Level 0 modular monolith, standalone-режим |
| Component Contracts | `components/*/contract/` (component_contract.json + openapi.yaml) | существует: 7/7, machine-readable |
| Contract-test suite | `tests/` (contract/boundary/conformance наборы) | существует: 811 тестов |
| Quality Gate | `scripts/quality-gate.ps1` (локальный) + `.github/workflows/quality-gate.yml` (CI) | GREEN: CI workflow активен; `main` `de13ab3...` успешно прошёл Quality Gate (run `34582747555`) |
| Маршрутизация ответственности | `.github/CODEOWNERS` | существует |

`docs/OPERATING_MODEL.md` — канонический операционный источник: он определяет, как команда (ChatGPT / Arena) работает через GitHub, и не создаёт архитектурных законов (Level A, §34.1 ARCHITECTURE.md; ADR не требуется). Архитектурным источником истины остаётся только `docs/ARCHITECTURE.md`, поэтому Operating Model не формирует второй архитектурный контур: при конфликте приоритет у ARCHITECTURE.md (см. §13 OPERATING_MODEL.md).

## 3. Проверка графа документации

### 3.1. Устранённые фиктивные ссылки

Следующие артефакты не существуют в подтверждённой истории и поэтому не используются как нормативные источники:

- `ADR-0009`;
- `docs/07-core-model.md`;
- `docs/08-scenario-validation.md`;
- `docs/01-architecture.md`;
- `docs/04-tech-stack.md`;
- `docs/adr.md`;
- `docs/architecture-review-1.1.0.md`.

`RELEASE_POLICY.md` также отсутствует, но это не является дефектом baseline: ARCHITECTURE.md 1.2.0 определяет его как отдельную операционную политику, необходимую при соответствующем режиме работы, а не как обязательный фундаментальный артефакт текущего standalone-режима.

### 3.2. Историческая целостность

ADR-0009 не восстанавливается задним числом.

Сведения, ранее приписанные ADR-0009, были перенесены в ADR-0010 только там, где они являются частью принятого решения 1.2.0. Неподтверждённые исторические утверждения о проведённых работах удалены из действующей архитектуры.

### 3.3. README

README синхронизирован с архитектурой 1.2.0 и больше не ссылается на удалённый аудит 1.1.0 и не утверждает, что ADR-0010 ожидает ратификации.

## 4. Ожидаемые артефакты

Обязательные для текущего foundation/standalone-режима:

- `docs/ARCHITECTURE.md`;
- `docs/adr/`;
- `docs/DOCUMENTATION_BASELINE.md`;
- `docs/OPERATING_MODEL.md`;
- `README.md`.

Ожидаемые по мере развития репозитория, но не обязательные до появления соответствующей потребности:

- `RELEASE_POLICY.md` — при переходе к соответствующему операционному режиму.

Появившиеся с переходом к реализации (необязательные для foundation-режима, но уже существующие и охраняемые):

- `.github/CODEOWNERS` — существует;
- Component Contracts — существуют (7/7);
- contract-test suite — существует;
- CI/conformance automation — активна: `.github/workflows/quality-gate.yml` выполняется для pull request и push в `main`; текущий `main` успешно прошёл Quality Gate. Branch protection/rulesets в рамках этого baseline не утверждаются.

Отсутствие необязательного артефакта не делает baseline `DIRTY`.

## 5. Состояние репозитория

**Существует (факт на момент этой актуализации):**

- 7 реализованных компонентов Level 0 (in-memory, standalone): `authorization`, `identity`, `tenant_authority`, `records`, `learning` (SCS-001: Slices 1–4 по ADR-0011 + Student Enrollment по ADR-0012, компонент версии 0.3.0), `saga`, `idempotency`;
- Component Contracts (7/7) с OpenAPI-документами;
- contract-test suite и поведенческие тесты (811 тестов: границы, tenant isolation, fail closed, idempotency, saga, audit, observability, enrollment);
- Quality Gate: локальный скрипт и активный CI workflow `.github/workflows/quality-gate.yml`; текущий `main` `de13ab3...` успешно прошёл Quality Gate (run `34582747555`).

**По-прежнему не существует** (фабричные механизмы не введены, что допустимо в standalone/foundation-режиме §4.1):

- Component Catalog;
- Component Registry;
- Composer;
- Golden Bundle;
- Platform Instance implementation;
- migration sets (физических БД нет — in-memory stores Level 0);
- deployable-артефакты (текущие `deployment.py` — только композиционный код).

Следствие для §30: ни один компонент не является `PUBLISHABLE` (отсутствуют migrations, deployable-артефакт, event-инфраструктура). Проект публикации не заявляет; это задокументированное состояние foundation/standalone-режима, а **не нарушение** архитектурного закона.

## 6. Что означает CLEAN

`CLEAN` разрешает перейти к следующему этапу:

```text
Documentation Baseline
        ↓
First Implementation Slice
        ↓
Component Contract
        ↓
Implementation
        ↓
Tests / Conformance
```

`CLEAN` не означает автоматического выбора технологий и не отменяет ADR для изменений уровня C/D.

## 7. Правило следующего изменения

Любое новое фундаментальное требование, отсутствующее в ARCHITECTURE.md 1.2.0, не должно появляться как «решение по умолчанию». Оно либо относится к реализации уровня A/B, либо оформляется соответствующим ADR.

Первый implementation slice должен быть минимальным, самостоятельно проверяемым и не требовать преждевременного включения фабричной механики.

## 8. Итог

**DOCUMENTATION BASELINE: CLEAN.**

Фундамент документации сопровождает репозиторий от конституционной подготовки до текущего реализованного состояния: компоненты Level 0 реализованы, контракты и contract-тесты существуют, Quality Gate воспроизводим и зелёный, фабричные механизмы намеренно не введены (standalone-режим, §4.1), компоненты не публикуются и `NOT PUBLISHABLE` по §30 задокументировано как текущее состояние foundation/standalone-режима.
