# Golden Bundle — Slice D

**Статус:** `IMPLEMENTED` (Slice D, Issue #71)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §7, §11, §15
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0 §14

---

## 1. Назначение

Golden Bundle — сертифицированный, воспроизводимый набор совместимых
компонентов и версий (ARCHITECTURE.md §14), предназначенный как известный
хороший состав (known-good composition) (ADR-0015 §7).

Bundle **фиксирует, идентифицирует, проверяет и обеспечивает
воспроизводимость** набора. Он **не** решает зависимости, **не** выбирает
версии, **не** подставляет версии и **не** собирает платформу
(ARCHITECTURE.md §17, ADR-0015 §7–§8) — это границы Composer (Slice E).

A Golden Bundle must make its component versions and compatibility identity
explicit (ADR-0015 §7). Он не заменяет контракты компонентов: он
упаковывает подтверждённые отношения совместимости между независимо
версионируемыми компонентами.

Bundle **не** становится вторым источником истины для метаданных
компонентов. Компонентная идентичность, версия, класс, владелец, артефакт
и зависимости остаются в каноническом Component Registry и опубликованных
контрактах (ADR-0015 §4, §7, §11). Bundle ссылается на них и отвергается
валидацией при расхождении.

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Схема | `factory/golden_bundle/schema/golden_bundle.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Загрузка, сборка, валидация | `src/golden_bundle/` | программный доступ, детерминированная сборка и проверка |
| Тесты | `tests/test_golden_bundle.py` | проверка архитектурных правил Slice D |
| CLI | `python -m golden_bundle` | validate, digest, build-example |
| Пример | `factory/golden_bundle/example_bundle.json` | эталонный bundle полного инвентаря реестра |

Конкурирующие bundle-каталоги не создаются в `factory/`. Сам bundle —
per-composition артефакт: его каноническое хранение определяет потребитель,
но формат и валидация — едины.

---

## 3. Формат

Минимальный bundle (draft, один компонент без зависимостей):

```json
{
  "$schema": "factory/golden_bundle/schema/golden_bundle.schema.json",
  "bundle_id": "compat-core",
  "bundle_version": "1.0.0",
  "bundle_digest": "sha256:...",
  "lifecycle": { "state": "draft" },
  "components": [
    {
      "component_id": "tenant_authority",
      "component_version": "0.1.0",
      "artifact": { "artifact_type": "none", "digest": null, "pinned": false, "canonical_form": null }
    }
  ],
  "compatibility": { "pairs": [] },
  "certification": null
}
```

Полный набор полей:

* `bundle_id` — стабильная идентичность линейки bundle (lower snake_case /
  kebab-case), никогда не floating selector.
* `bundle_version` — явная SemVer версия bundle.
* `bundle_digest` — `sha256:` + 64 hex, digest канонического JSON контента
  bundle без самого поля `bundle_digest` и без `$schema`. Вычисляется
  детерминированно (`sort_keys=True`, компактные разделители), так что
  одинаковый `bundle_id + bundle_version` при одинаковом контенте всегда
  даёт одинаковый digest, а любое изменение контента меняет digest.
* `lifecycle.state` — одно из `draft → candidate → certified → deprecated →
  revoked` (ARCHITECTURE.md §14).
* `components` — массив конкретных компонентов, отсортированный по
  `component_id` для воспроизводимости. Каждый элемент содержит
  `component_id`, `component_version` (явный SemVer), `artifact`
  (`artifact_type`, `digest`, `pinned`).
* `compatibility.pairs` — явная machine-readable матрица проверенных
  попарных отношений совместимости: `{ from, to, mechanism, compatible }`,
  отсортированная по `(from, to, mechanism)`.
* `certification` — `null` до сертификации; обязательна для `certified` и
  далее: `{ certified_by, certified_at (ISO8601 UTC), certification_id,
  checks }`. `certified` означает прохождение утверждённого набора проверок
  совместимости и безопасности (ARCHITECTURE.md §14).

---

## 4. Lifecycle

```text
draft → candidate → certified → deprecated → revoked
```

Правила:

* состояния явные: `draft` не означает `candidate`, `candidate` не означает
  `certified`, `certified` не означает `deprecated`;
* переходы разрешены только на следующий шаг (строго линейно) либо
  сохранение того же состояния (идемпотентно); пропуск шагов отвергается;
* `certified` и далее требуют `certification`-evidence;
* состояние до `certified` не должно содержать evidence о сертификации;
* `revoked` запрещает новые поставки, но не означает автоматического
  разрушения уже работающих инстансов (ARCHITECTURE.md §14); этот slice не
  выполняет поставку и не трогает инстансы.

---

## 5. Immutability

Поставляемый bundle иммутабелен и идентифицируется
`bundle_id + version + digest` (ARCHITECTURE.md §14). Проверка
`check_immutability` отвергает новую версию под тем же
`bundle_id + bundle_version` у уже поставляемого (certified и далее) bundle,
если контент/digest изменились. Silent replacement и silent mutation не
допускаются.

---

## 6. Authoritative inputs

Bundle ссылается на явные, детерминированные, проверяемые authoritative
источники:

* `factory/registry/component_registry.json` — канонический реестр;
* `factory/catalog/component_catalog.json` — производный discoverable view.

Валидация проверяет (fail-closed — недоступный/невалидный реестр
отвергает bundle):

* каждый `component_id` зарегистрирован в реестре;
* каждый `component_version` совпадает с версией, зарегистрированной в
  реестре для этого `component_id`;
* `artifact` повторяет authoritative identity реестра, не выдумывая и не
  переопределяя её;
* нет дубликатов компонентов;

Bundle не копирует всю информацию Registry и не создаёт второй источник
истины.

---

## 7. Dependencies and compatibility

Bundle **проверяет** заявленные зависимости между закреплёнными
компонентами и **фиксирует** проверенные отношения совместимости. Он **не**
обнаруживает зависимости, **не** разрешает их и **не** подставляет версии
(ARCHITECTURE.md §17; ADR-0015 §7–§8).

Проверки:

* для каждого закреплённого компонента каждая зависимость, объявленная
  каноническим реестром, обязана быть закреплена в bundle; отсутствующая
  или выпадающая из объявленного диапазона версия — детерминированная
  ошибка, а не неявная подстановка;
* каждая объявленная зависимость обязана быть записана в матрицу
  совместимости с вердиктом `compatible: true` — Golden Bundle упаковывает
  *подтверждённые совместимые* отношения (ADR-0015 §7);
* запрещены записи матрицы, не соответствующие объявленной зависимости
  канонического реестра (bundle не выдумывает зависимости);
* запрещены `database`, `internal_code`, `private_schema`, `internal_queue`
  как механизмы (ARCHITECTURE.md §18) — у них нет представления в схеме.

---

## 8. Artifact pinning

* `artifact_type`: `none` (нет публикуемого артефакта, Level 0),
  `container_image`, `source_package`.
* Если `artifact_type == "none"`: `digest == null`, `pinned == false`.
* Если `artifact_type != "none"`: `digest` — обязательный immutable sha256,
  `pinned == true` (ARCHITECTURE.md §1.3, §11).
* Отсутствие обязательного digest — ошибка. Floating selectors запрещены
  как digest.

---

## 9. Reproducibility / Determinism

* одинаковые authoritative inputs → одинаковый канонический вид → одинаковый
  `bundle_digest` (ARCHITECTURE.md §11; ADR-0015 §6);
* детерминированность: `sort_keys=True` при вычислении digest, сортировка
  `components` по `component_id`, сортировка `compatibility.pairs` по
  `(from, to, mechanism)`, отсортированный список ошибок;
* отсутствует зависимость от порядка обхода файлов, случайных значений,
  текущего времени, локального окружения и сети; валидация не зависит от
  `now`.

---

## 10. Validation

```python
from golden_bundle import validate_document, load_bundle, build_bundle_document

errors = validate_document(document, root=root)   # [] когда валиден
bundle = load_bundle(path)                        # загружает и валидирует
doc = build_bundle_document(
    bundle_id="compat-core",
    bundle_version="1.0.0",
    lifecycle_state="draft",
    components=[...],
    compatibility_pairs=[...],
)
```

CLI:

```bash
python -m golden_bundle validate path/to/bundle.json
python -m golden_bundle digest path/to/bundle.json
python -m golden_bundle build-example
```

Запуск focused-тестов:

```bash
python -m pytest tests/test_golden_bundle.py
```

---

## 11. Forbidden selectors

Запрещены как production selectors:

```text
latest  current  default  stable  edge  main  master  head  tip  *
```

Проверка применяется к полям, которые реально выполняют выбор: `bundle_id`,
`bundle_version`, `bundle_digest`, `component_id`, `component_version`,
`artifact.digest`, `artifact_type`, `lifecycle.state`, парам
`compatibility`. Слово `latest` остаётся допустимым в prose — в тестах,
фикстурах, сообщениях об ошибках и документации, где оно ничего не выбирает.

---

## 12. Чего здесь нет

Slice D реализует только Golden Bundles. В этом slice нет:

* Slice E — Composer, automatic platform composition, dependency resolution,
  version selection, assembly orchestration;
* deployment / platform provisioning / rollout / release train;
* новой системы хранения артефактов;
* второго источника истины для метаданных компонентов;
* прямого доступа к внутренним БД компонентов;
* internal cross-component imports.

Bundle может быть закреплён ссылкой в Platform Manifest, но сам не
реализует сборку платформы.
