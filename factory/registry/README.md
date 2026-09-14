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

## 6. Контракты

Запись реестра ссылается на опубликованные контракты и не копирует их
содержание. Валидация проверяет, что:

* файл контракта существует;
* контракт объявляет тот же `component_id`;
* контракт объявляет ту же `component_version`;
* ссылка не выходит за пределы репозитория.

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
