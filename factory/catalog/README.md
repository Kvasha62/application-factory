# Component Catalog — Slice B

**Статус:** `IMPLEMENTED` (Slice B, Issue #64)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §5, §11, §15
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0

---

## 1. Назначение

Component Catalog — discoverable view компонентов, доступных фабрике
(ADR-0015 §5).

Каталог является **производным представлением (derived view)**, а не вторым
источником истины. Каждое значение каталога вычисляется из канонического
Component Registry (Slice A) и опубликованных Component Contracts.
Каталог не владеет бизнес-данными компонентов, не дублирует их и не даёт
права доступа к внутренностям какого-либо компонента.

Composition-решения в конечном счёте опираются на machine-readable
authoritative metadata — канонический реестр и опубликованные контракты.
Каталог открывает их для обнаружения и фильтрации, но не подменяет.

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Каталог | `factory/catalog/component_catalog.json` | **единственный** canonical machine-readable каталог |
| Схема | `factory/catalog/schema/component_catalog.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Derivation, валидация, discovery | `src/component_catalog/` | детерминированная генерация, проверка и фильтрация |
| Тесты | `tests/test_component_catalog.py` | проверка архитектурных правил |

Конкурирующие каталоги не создаются. Документ каталога
**машинно-генерируемый**: ручные правки отвергаются валидацией, поскольку
документ обязан совпадать с детерминированным результатом derivation.

---

## 3. Derivation

Каталог строится только из канонических источников:

```text
factory/registry/component_registry.json   (Slice A, canonical registry)
components/<component_id>/contract/component_contract.json   (published contracts)
        ↓  derive (детерминированная функция)
factory/catalog/component_catalog.json     (derived view)
```

Regeneration:

```bash
python -m component_catalog build
python -m component_catalog validate
```

Derivation — чистая функция канонических источников:

* записи следуют в canonical registry order;
* `lifecycle` и `contracts` копируются из записи реестра verbatim;
* `summary` зеркалирует поле `task` опубликованного контракта verbatim;
  `null` означает, что контракт не декларирует task — честное отсутствие,
  а не выдуманное описание;
* `entry_digest` — единственное вычисляемое значение (см. §5);
* ничего не выдумывается и не редактируется руками.

---

## 4. Состав записи

| Поле | Содержание |
|---|---|
| `component_id` | стабильная идентичность, зеркальная реестру |
| `component_version` | явная публичная SemVer-версия, зеркальная реестру |
| `class` | класс компонента (`ARCHITECTURE.md` §3), зеркальный реестру |
| `scs_id` | SCS-идентичность Business System или `null`, зеркальная реестру |
| `maturity_level` | уровень зрелости, зеркальный реестру |
| `owner` | ответственный владелец, зеркальный реестру |
| `data_scopes` | области данных компонента, зеркальные реестру |
| `lifecycle` | lifecycle/release состояние, скопированное verbatim |
| `contracts` | ссылки на опубликованные контракты, скопированные verbatim |
| `summary` | task-описание из опубликованного контракта или `null` |
| `entry_digest` | sha256-digest всей записи канонического реестра |

Верхний уровень содержит `catalog_id`, `catalog_schema_version` и
`derived_from` — явную ссылку на канонический реестр (`canonical_registry`,
`registry_id`, `component_contracts_root`).

---

## 5. Digest и authoritative linkage

Каждая запись каталога закреплена за своей записью реестра явным
content digest:

```text
entry_digest = "sha256:" + sha256(canonical_json(registry_entry))
```

где `canonical_json` — сериализация с сортировкой ключей и компактными
разделителями. Digest покрывает **всю** запись реестра, включая поля, которых
в каталоге нет (dependencies, compatibility, artifact, datasets). Любое
изменение записи реестра делает digest каталога устаревшим — и валидация
отвергает каталог как разошедшийся с canonical metadata.

Authoritative linkage:

* `derived_from` ссылается на канонический реестр и его `registry_id`;
* `contracts` ссылаются на опубликованные контракты;
* программно полная authoriative metadata доступна через
  `Catalog.registry_entry(component_id)` — реестр остаётся единственным
  источником полных метаданных.

---

## 6. Discovery и фильтрация

Machine-readable discovery (Issue #64) — пять измерений, необходимых для
composition-решений:

```python
from component_catalog import load_catalog

catalog = load_catalog()
catalog.component_ids                     # каталогизированный инвентарь
catalog.entry("booking")                  # discoverable view одного компонента
catalog.registry_entry("booking")         # полная запись canonical registry

catalog.filter(component_class="business_system")
catalog.filter(data_scope="tenant-scoped")
catalog.filter(lifecycle_state="registered")
catalog.filter(owner="@Kvasha62")
catalog.filter(scs_id="SCS-001")
catalog.filter(component_class="business_system", data_scope="tenant-scoped")
```

Правила фильтрации:

* закрытые словари (`component_class`, `data_scope`, `lifecycle_state`)
  отвергают недопустимые значения ошибкой, а не пустым результатом;
* открытые значения (`owner`, `scs_id`) могут законно давать пустой результат;
* floating selector (`latest` и т.п.) не принимается как значение фильтра;
* результат сохраняет canonical registry order.

Каталог не выполняет composition, version resolution и не выбирает версии —
это границы Slice C/D/E.

---

## 7. Валидация

Валидация детерминирована, возвращает **все** нарушения сразу (отсортированы)
и проверяет, что каталог остаётся derived view:

```python
from component_catalog import validate_document

validate_document(document, root=root)   # список нарушений, [] когда валиден
```

Проверяются:

* schema (нормативная JSON Schema каталога);
* канонический реестр сам загружается и валиден — каталог не может быть
  производным от невалидных источников;
* `derived_from` ссылается на канонический реестр;
* inventory parity: каталог описывает ровно зарегистрированный инвентарь,
  в canonical registry order, без выдуманных и молча пропущенных компонентов;
* каждое зеркальное поле равно своему источнику в реестре;
* каждый `entry_digest` закрепляет текущую запись реестра;
* каждый `summary` зеркалит `task` опубликованного контракта;
* весь документ совпадает с детерминированным результатом derivation —
  любое расхождение (ручная правка, устаревший снимок, переставленные
  записи) отвергается.

Запуск focused-тестов:

```bash
python -m pytest tests/test_component_catalog.py
```

---

## 8. Идемпотентность `latest`

Запрещено использование `latest` (и других floating selectors) как способа
выбора версии, релиза, артефакта, зависимости или production-цели
(`ARCHITECTURE.md` §1.3; ADR-0015 §13). Проверка применяется к полям,
которые реально выполняют выбор: `component_id`, `component_version`,
значения фильтров discovery.

Слово `latest` остаётся допустимым в prose — в тестах, фикстурах, сообщениях
об ошибках и документации, где оно ничего не выбирает.

---

## 9. Чего здесь нет

Slice B реализует только derived catalog. В этом каталоге и во всём Slice B
нет:

* Platform Manifest (Slice C) — не реализован;
* Golden Bundles (Slice D) — не реализованы;
* Composer (Slice E) — не реализован;
* platform/customer composition и assembly;
* deployment orchestration;
* отдельной системы version resolution;
* второго источника истины для метаданных компонентов;
* прямого доступа к внутренним БД компонентов;
* internal cross-component imports.

Каталог не расширяет архитектуру: `ARCHITECTURE.md` и ADR-0015 не изменены.
