# Component Registry — Slice A

**Статус:** `IMPLEMENTED` (Slice A, Issue #62)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §4, §11, §15
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0

---

## 1. Назначение

Component Registry — канонический machine-readable реестр компонентов фабрики.

Реестр содержит **только factory-level metadata**. Он не является владельцем
бизнес-данных компонентов и не становится вторым источником истины для
опубликованных контрактов (ADR-0015 §4, §11).

Реестр описывает компоненты. Он не владеет их бизнес-фактами.

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Реестр | `factory/registry/component_registry.json` | **единственный** canonical machine-readable реестр |
| Схема | `factory/registry/schema/component_registry.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Загрузка и чтение | `src/component_registry/` | программный доступ и валидация |
| Тесты | `tests/test_component_registry.py` | проверка архитектурных правил |

Конкурирующие реестры не создаются. Копирование реестра в другое место
является нарушением правила «одна тема — один канонический документ».

Каждая запись реестра выведена из опубликованного контракта компонента
`components/<component_id>/contract/component_contract.json`. Реестр не
выдумывает metadata: любое расхождение между записью и её контрактом
обнаруживается валидацией как ошибка.

---

## 3. Состав записи

Каждая запись реестра содержит:

| Поле | Содержание |
|---|---|
| `component_id` | стабильная уникальная идентичность компонента |
| `component_version` | явная публичная SemVer-версия |
| `class` | класс компонента (`ARCHITECTURE.md` §3) |
| `owner` | ответственный владелец компонента |
| `contracts` | ссылки на опубликованные контракты |
| `data_ownership` | владение данными и области данных |
| `dependencies` | объявленные зависимости и их версионные ограничения |
| `compatibility` | machine-readable compatibility metadata |
| `artifact` | явная идентичность артефакта |
| `lifecycle` | явное lifecycle/release состояние |

Дополнительно: `maturity_level`, `scs_id` — перенесены из опубликованных
контрактов без изменения.

---

## 4. Идентичность

Идентичность компонента — `component_id`:

* **stable** — lower snake_case, не зависит от регистра, пробелов и позиции;
* **unique** — дубликат отклоняется валидацией;
* **deterministic** — одна и та же идентичность всегда разрешается ровно
  в одну запись.

Алиасы не создают второй канонической идентичности. Идентичность не является
версией и не может быть floating selector-ом.

---

## 5. Версионирование

Публичная версия компонента — явный SemVer `MAJOR.MINOR.PATCH`
(`ARCHITECTURE.md` §11).

Запрещено как selector:

```text
latest  current  default  stable  edge  main  master  head  tip  *
```

Запрещены:

* implicit version selection;
* floating production version;
* silent version substitution.

> Запрещено не слово `latest`, а его использование как selector-а — как способ
> выбрать версию, релиз, артефакт, зависимость или production-цель.
> Слово остаётся допустимым в тестах, фикстурах, сообщениях об ошибках,
> комментариях и документации, где оно ничего не выбирает.

Проверка применяется к полям, которые реально выполняют выбор:
`component_id`, `component_version`, `dependencies[].version_range`,
`dependencies[].component_id`, `artifact.digest`, `artifact.artifact_type`,
`lifecycle.registry_state`, `compatibility.api_policy`,
`compatibility.event_policy`.

---

## 6. Ссылки на контракты

Ссылки реестра — это не произвольные пути файловой системы, а
**repository-relative canonical references**: идентичность документа
определяется каноническим путём, который архитектура назначает компоненту,
а не файлом, который просто объявляет подходящий `component_id` в другом
месте. Дубликат или decoy с корректным содержимым, но не по каноническому
пути, отвергается.

Canonical path компонента `<component_id>`:

```text
components/<component_id>/contract/component_contract.json
components/<component_id>/contract/openapi.yaml
```

Все пути:

* относительные от repository root;
* нормализованные (отвергаются `./`, `//`, завершающий `/`);
* без обходов (`..`) и без абсолютных путей;
* без URL и схем (`https:`, `file:`, `C:`);
* без symlink-like и normalization tricks.

### `contracts.component_contract`

Должен быть ровно canonical path contract-а **этого же** компонента:

```text
components/<component_id>/contract/component_contract.json
```

Fragment (`#...`) **не разрешён**: ссылка адресует весь документ.

### `data_ownership.source` и `compatibility.source`

Состоят из двух частей:

```text
DOCUMENT_PATH#FRAGMENT
```

```text
components/<component_id>/contract/component_contract.json#/data_ownership
components/<component_id>/contract/component_contract.json#/compatibility_policy
```

* `DOCUMENT_PATH` — canonical contract того же компонента;
* `FRAGMENT` — валидный JSON Pointer (RFC 6901), который реально разрешается
  в этом документе и адресует объект ожидаемого раздела.

Fragment является частью семантики ссылки. Проверяется не только
существование файла, но и:

* наличие ровно одного fragment-а;
* canonical document path;
* корректность и разрешимость JSON Pointer;
* соответствие ожидаемому semantic target.

### `dependencies[].contract`

Должен быть canonical path contract-а **целевого** компонента:

```text
components/<dependency.component_id>/contract/component_contract.json
```

Дополнительно проверяется, что этот canonical contract объявляет ровно ту
версию, которая зарегистрирована для целевого компонента. Расхождение версии
— ошибка валидации, даже если диапазон зависимости её формально допускает.

### Содержание контракта

Также проверяется, что canonical contract объявляет тот же `component_id`
и ту же `component_version`, что и запись реестра, и что metadata реестра
не расходится с опубликованным контрактом.

---

## 7. Зависимости

Зависимость объявляется machine-readable и содержит целевую идентичность,
явное версионное ограничение и механизм взаимодействия.

Допустимые механизмы (`ARCHITECTURE.md` §18): `api`, `events`,
`data_export_cdc`, `internal-consumer-surface`.

Запрещённые механизмы: `database`, `internal_code`, `private_schema`,
`internal_queue`.

Зависимости **не** обнаруживаются через internal imports и internal DB queries.
Реализация реестра читает только опубликованные контракты и сам реестр.

---

## 8. Владение данными

Реестр описывает владение, но не передаёт и не дублирует его:

* `owner` — ответственный владелец компонента;
* `data_ownership.owner` — компонент, владеющий данными (LAW-03);
* `data_ownership.data_scopes` — области данных (`ARCHITECTURE.md` §5.4,
  LAW-16a): `tenant-scoped`, `platform-scoped`, `system-scoped`.

Реестр не владеет: курсами и enrollments Learning; продуктами, корзинами
и заказами Commerce; ресурсами и резервированиями Booking; внутренностями
Identity, Tenant Authority, Authorization; данными Records; состоянием Saga;
состоянием Idempotency.

---

## 9. Артефакты

`ARCHITECTURE.md` §11 требует для поставляемого артефакта `id + version + digest`.

На Level 0 deployable-артефакты отсутствуют
(`ARCHITECTURE.md` §30; `CONFORMANCE_REVIEW_1.2.0` DOD-02), поэтому каждая
запись явно фиксирует:

```json
"artifact": { "artifact_type": "none", "digest": null, "pinned": false }
```

Это явное утверждение об отсутствии артефакта, а не пропуск поля:
`digest` не может быть `latest`, а опубликованный артефакт обязан быть
pinned по digest.


## 9.1 Canonical representation and artifact boundary — F-3B Deliverables 1–2 (Issue #84)

Per ADR-0018 §3.1,§4,§5,§8,§13,§16,§21,§25-27,§31-32, ARCHITECTURE.md §11,§16,§31,§37.1. Это Factory/Registry contract как deliverable Issue #84, не изменение архитектуры и не implementation. `source_package` и `container_image` сейчас не публикуются (все entries `none` per §9) — контракт определяет как Factory должна определять canonical форму когда они будут публиковаться.

### source_package

- canonical representation — DELIVERABLE: immutable canonical representation defined by Factory; for source_package it MAY be realized as deterministic file set / archive, concrete form is a deliverable of #84, not an architectural mandate. Digest input includes content bytes, file names, directory/structural information, all in deterministic canonical order; timestamps and owner bits not part of executable semantics do NOT enter identity; metadata that really affects executable semantics MUST NOT be arbitrarily excluded from canonical representation. Technology-neutral, no specific archive format mandated.
- sealed artifact unit — весь canonical representation как опубликован, identity = `artifact.digest` per §3.1,§4.
- artifact boundary — DELIVERABLE: which component-owned executable bytes are inside sealed unit as defined by Factory; physical presence of a file inside canonical representation is NOT sufficient proof of ownership; only content that Factory defines as part of closed executable unit enters boundary; non-owned content inside sealed artifact is allowed only if non-executable; executable content inside sealed artifact MUST BE component-owned per Factory contract; Factory MUST NOT define executable content inside sealed artifact as non-owned; if ownership cannot be proven — fail-closed per §16.

### container_image

- canonical representation — DELIVERABLE: immutable image artifact defined by Factory; digest is identity of sealed unit, not only entrypoint. Concrete OCI layout is implementation detail, not an architectural requirement; concrete form is a deliverable of #84. Digest input same rules as for source_package: content bytes + file names + directory/structural information (image structure) in deterministic canonical order; timestamps/owner bits not part of executable semantics do NOT enter identity; executable-relevant metadata MUST NOT be arbitrarily excluded. Technology-neutral, no specific OCI layout mandated.
- artifact boundary — DELIVERABLE: same rules as source_package: boundary = component-owned executable bytes inside canonical as defined by Factory; physical presence NOT sufficient; non-owned only if non-executable; executable MUST BE owned; MUST NOT define executable as non-owned; fail-closed if cannot prove.
- host/container runtime — не является частью boundary per ADR-0018 §8.

### Cross-type invariants

- `artifact.digest` = identity sealed artifact unit, не identity execution graph — §4
- Factory — единственный source of truth для canonical, boundary, digest, reproducibility — §5, §37.1
- Manifest/Instance наследуют identity, не создают и не расширяют boundary, не хранят native graph / DT_NEEDED — §10,§11, Alt A/B rejected §23
- D&O получает authoritative contract, проверяет, fail-closed, не создаёт boundary, не добавляет runtime-discovered — §6,§14,§16
- Workspace, host search path, runtime-discovered files — не доказательство trust — §16,§17,§28
- `artifact_type=none` — digest null, no artificial digest, не часть F-3B, при требовании F-3B → отказ per §16 — §9.1
- `deployed` = ADR-0016 §10 + ADR-0017 §33-35, не изменяется — §9.1,§20,§32
- Runtime discovery не расширяет trusted closure, DT_NEEDED может использоваться как enforcement mechanism, но не как source of truth — §6

### What this contract does NOT prescribe

- конкретный архивный формат (tar/zip/directory)
- конкретный OCI/runtime implementation
- ELF parser как обязательная архитектура
- Python import hook как обязательная архитектура
- DT_NEEDED в Manifest/Instance
- native dependency graph в Manifest/Instance
- VEB как source of truth
- runtime auto-add / first-object-only verification

---

## 10. Lifecycle

`lifecycle.registry_state` описывает состояние **записи реестра**:

| Состояние | Смысл |
|---|---|
| `registered` | запись присутствует в каноническом реестре |
| `deprecated` | запись выведена из обращения |
| `retired` | запись снята с учёта |

> `registered` **не означает** автоматически deployable или publishable.

Реестр ничего не сертифицирует. Компоненты Level 0 не являются `PUBLISHABLE`
по `ARCHITECTURE.md` §30: отсутствуют migrations и deployment artifact.
Это состояние зафиксировано явно через `deployable`, `publishable`
и `publishability_blockers`.

---

## 11. Валидация

Валидация детерминирована и возвращает **все** нарушения сразу, а не первое.

```python
from component_registry import load_registry

registry = load_registry()          # загружает и валидирует canonical registry
registry.component_ids              # зарегистрированный инвентарь
registry.version("learning")        # явная версия
registry.dependencies("learning")   # объявленные зависимости
registry.validate()                 # [] когда реестр валиден

from component_registry import validate_document
validate_document(document, root=root)   # список нарушений, отсортирован
```

Проверяются: schema, identity, identity uniqueness, version, contract
references, dependencies, compatibility, ownership, artifact identity,
lifecycle.

Запуск focused-тестов:

```bash
python -m pytest tests/test_component_registry.py
```

---

## 12. Чего здесь нет

Slice A реализует только реестр. В этом каталоге и во всём Slice A нет:

* Component Catalog (Slice B) — не реализован;
* Platform Manifest (Slice C) — не реализован;
* Golden Bundles (Slice D) — не реализованы;
* Composer (Slice E) — не реализован;
* deployment orchestration;
* release-train automation;
* cross-component DB access;
* internal cross-component imports.

Реестр не расширяет архитектуру: `ARCHITECTURE.md` и ADR-0015 не изменены.
