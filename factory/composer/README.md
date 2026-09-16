# Composer — Slice E

**Статус:** `IMPLEMENTED` (Slice E, Issue #73)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §8, §10, §11, §15
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0 §16, §17, §18, §20, §21

---

## 1. Назначение

Composer реализует `ARCHITECTURE.md` §17: разрешает зависимости, проверяет
совместимость, выбирает версии, применяет configuration, проверяет extensions,
формирует воспроизводимый Manifest и передаёт его на validation.

Slice E — это **детерминированная композиция и детерминированное отклонение**
несовместимых или contract-invalid сборок платформы (ADR-0015 §15). Composer
собирает платформу только из опубликованных factory-метаданных:

* канонический Component Registry (`factory/registry/`);
* опубликованные Component Contracts (`components/*/contract/`);
* Golden Bundle как certified known-good composition (`factory/golden_bundle/`).

Composer **не** является вторым источником истины, **не** владеет бизнес-данными,
**не** читает внутренние БД компонентов, **не** импортирует внутренние модули
компонентов, **не** изобретает версии и **не** скрыто заменяет состав
(ADR-0015 §8, §11).

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Схема запроса | `factory/composer/schema/composition_request.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Правила | `factory/composer/README.md` | этот документ |
| Пример | `factory/composer/example_request.json` | детерминированный эталонный запрос |
| Реализация | `src/composer/` | разрешение, выбор версий, проверки, сборка Manifest |
| Тесты | `tests/test_composer.py` | проверка архитектурных правил Slice E |
| CLI | `python -m composer` | validate, compose, build-example |

Composition request и Platform Manifest — per-platform артефакты: их
каноническое хранение определяет потребитель, но формат и валидация едины.
Пример в `factory/composer/` выводится из реестра детерминированной сборкой и
не является вторым источником истины: любое расхождение с authoritative
metadata отвергается.

---

## 3. Формат запроса

Минимальный запрос (один явно запрошенный компонент; остальное — closure
объявленных зависимостей):

```json
{
  "$schema": "factory/composer/schema/composition_request.schema.json",
  "manifest": { "manifest_id": "learning-platform", "manifest_version": "1.0.0" },
  "components": [
    { "component_id": "learning", "component_version": "0.3.0" }
  ]
}
```

Полный набор полей:

* `manifest` — identity будущего Platform Manifest: `manifest_id` (устойчивая
  идентичность линейки), `manifest_version` (явный SemVer), опциональный
  `predecessor` (`null` или `{ manifest_id, manifest_version, manifest_digest? }`).
  `predecessor` передаётся в манифест и валидируется Platform Manifest
  validation (§16); Composer не дублирует эти правила и не переписывает lineage.
* `components` — явно запрошенные компоненты, отсортированные по
  `component_id`. Каждый элемент содержит **ровно одно** из:
  `component_version` (явный pin) или `version_range` (явная constraint вида
  `>=0.3.0,<0.4.0`). Floating selectors запрещены.
* `golden_bundle` — `null` либо ссылка
  `{ bundle_id, bundle_version, bundle_digest, path }` на certified Golden Bundle.
* `configuration` — map `component_id → configuration компонента`
  (`ARCHITECTURE.md` §20).
* `extensions` — расширения через опубликованный контракт компонента
  (`ARCHITECTURE.md` §21).
* `branding` — опциональные branding-метаданные.

Artifact identity (тип, digest, pinned) в запросе **не указывается**: она
повторяется из authoritative реестра (`ARCHITECTURE.md` §1.3, §11).

---

## 4. Разрешение зависимостей и выбор версий

Кандидатный набор — authoritative Component Registry. Канонический реестр
публикует одну конкретную публичную версию на компонент (`ARCHITECTURE.md`
§11); Composer не вводит собственный индекс доступных версий и не ищет версии
где-либо ещё.

Правила:

* явный `component_version` обязан совпадать с версией, опубликованной
  реестром; иная версия — детерминированное отклонение, а не замена;
* `version_range` вычисляется только против зарегистрированной версии; если
  constraint её не допускает — детерминированное отклонение;
* после явно запрошенного набора Composer строит **closure объявленных
  зависимостей** реестра (`ARCHITECTURE.md` §18). Каждый добавленный компонент
  фиксируется как явное решение (`selected_as: dependency`,
  `required_by: [...]`), поэтому изменение состава никогда не скрыто
  (`ARCHITECTURE.md` §17);
* необъявленные зависимости не изобретаются: closure строится только из
  metadata реестра;
* ни одна версия не подставляется, не поднимается и не понижается, чтобы
  сборка «сошлась» (ADR-0015 §8).

Порядок детерминирован: явные компоненты в порядке `component_id`, затем
closure объявленных зависимостей; результат всегда отсортирован по
`component_id`.

---

## 5. Совместимость и contract validity

Проверки (ADR-0015 §8, §10, §15; `ARCHITECTURE.md` §1.4, §18):

* каждая объявленная зависимость каждого собранного компонента присутствует в
  сборке; выбранная версия обязана удовлетворять объявленному диапазону —
  иначе сборка отклоняется с объяснением несовместимости;
* запрещённые механизмы зависимостей (`database`, `internal_code`,
  `private_schema`, `internal_queue`) не могут быть способом удовлетворить
  зависимость (`ARCHITECTURE.md` §1.1, §18);
* опубликованный компонентный контракт каждого собранного компонента
  существует, называет ту же identity и ту же версию, что выбраны, и объявляет
  те же зависимости, что authoritative реестр. Реестр, расходящийся со своими
  контрактами, композиции не подлежит (LAW-05; ADR-0015 §15);
* data ownership сохраняется как опубликовано: Composer не переназначает
  владельца данных (LAW-03; ADR-0015 §11).

---

## 6. Configuration

`ARCHITECTURE.md` §20: конфигурация декларативна, версионируется вместе с
компонентом, а неизвестный configuration key — **BUILD ERROR**.

* configuration валидируется против `configuration_schema`, опубликованной в
  контракте соответствующего компонента;
* ключ, не объявленный компонентом, — ошибка, независимо от того, насколько
  permissive объявлена схема;
* обязательные ключи обязаны быть указаны явно;
* Composer **не подставляет** `default`-значения: значения, которых нет в
  запросе, остаются на компоненте и не появляются в манифесте;
* конфигурация компонента, которого нет в собранной платформе, — ошибка;
* секреты не имеют представления в конфигурации: неизвестный ключ отвергается
  (`ARCHITECTURE.md` §20, §27), а сам манифест секретов не содержит;
* незнакомое ключевое слово в опубликованной configuration schema —
  fail-closed, а не молчаливый пропуск.

---

## 7. Extensions

`ARCHITECTURE.md` §21: extension работает только через публичный контракт
компонента.

* расширение обязано называть `component_id`, который входит в собранную
  платформу; extension не вводит новый компонент;
* `mechanism` — только `webhook`, `custom_fields`, `ui_widget`, `theme`,
  `plugin`, `official_extension_api`;
* `contract` обязан быть одним из контрактов, опубликованных authoritative
  реестром для этого компонента. Ссылка на внутренний путь, чужой контракт или
  неопубликованный файл отвергается;
* расширения передаются в манифест и дополнительно проверяются Platform
  Manifest validation.

---

## 8. Golden Bundle как evidence сертификации

Если запрос ссылается на Golden Bundle, Composer:

* читает bundle по repository-relative `path` и валидирует его правилами
  Slice D (fail-closed);
* сверяет `bundle_id`, `bundle_version` и пересчитанный `bundle_digest` с
  заявленной ссылкой;
* требует lifecycle `certified`: `draft`/`candidate` ещё не несут
  сертификационного evidence, а `deprecated`/`revoked` не обслуживают новые
  поставки (`ARCHITECTURE.md` §14);
* требует, чтобы собранная платформа **точно совпадала** с pinned-набором
  bundle, и чтобы каждое объявленное отношение зависимости было записано в
  матрице совместимости как `compatible: true`. Сертификация bundle не
  наследуется другой композицией (ADR-0015 §7).

Без ссылки на bundle композиция является **явно uncertified**: это состояние
выражено `golden_bundle: null` в манифесте (`ARCHITECTURE.md` §31), а не
подразумеваемой сертификацией.

---

## 9. Производимый Manifest и передача на validation

Composer формирует Platform Manifest формата Slice C и передаёт его
Platform Manifest validation:

* lifecycle производимого манифеста — `draft`: Composer **не** утверждает, не
  публикует, не деплоит и не разворачивает Platform Instance. Approval,
  publication и deployment остаются актами владельца в lifecycle манифеста
  (`ARCHITECTURE.md` §16; ADR-0015 §8, §13);
* `manifest_digest` вычисляется той же канонической моделью, что и в Slice C;
* `components` повторяют authoritative artifact identity реестра и
  отсортированы по `component_id`;
* манифест, который Composer не может передать валидным, — отклонённая
  композиция, а не записанный артефакт.

Composer собирает платформу из одной составной части: Platform Manifest.
Сборка Platform Instance (provisioning, deployment, rollout) в Slice E не
входит.

---

## 10. Reproducibility / Determinism

* одинаковый запрос + одинаковое authoritative состояние → одинаковый
  манифест, одинаковый `manifest_digest`, одинаковый список решений;
* детерминированность: сортировка компонентов, сортировка решений,
  отсортированный список ошибок, отсутствие зависимости от порядка обхода
  файлов, случайных значений, текущего времени, локального окружения и сети;
* валидация не зависит от `now`;
* отклонение всегда объясняет нарушение совместимости или контракта
  (ADR-0015 §8).

---

## 11. Validation

```python
from composer import (
    build_request_document,
    compose_request_document,
    compose_diagnostics,
    validate_request_document,
)

errors = validate_request_document(document, root=root)   # [] когда запрос валиден
errors = compose_diagnostics(document, root=root)         # запрос + композиция
composition = compose_request_document(document, root=root)
composition.manifest_digest
composition.summary()                                     # отчёт о решениях
composition.render()                                      # детерминированный JSON
```

CLI:

```bash
python -m composer validate factory/composer/example_request.json
python -m composer compose factory/composer/example_request.json --out /tmp/manifest.json
python -m composer build-example
```

Запуск focused-тестов:

```bash
python -m pytest tests/test_composer.py
```

---

## 12. Forbidden selectors

Запрещены как production selectors:

```text
latest  current  default  stable  edge  main  master  head  tip  *
```

Проверка применяется к полям, которые реально выполняют выбор: `manifest_id`,
`manifest_version`, `predecessor`, `component_id`, `component_version`,
`version_range`, `golden_bundle` (id/version/digest), configuration,
extensions. Слово `latest` остаётся допустимым в prose — в тестах, фикстурах,
сообщениях об ошибках и документации, где оно ничего не выбирает.

---

## 13. Чего здесь нет

Slice E реализует только Composer. В этом slice нет:

* Platform Instance assembly: provisioning, deployment, rollout, release train;
* Platform Manifest approval/publication/deployment — это lifecycle Slice C;
* собственного индекса доступных версий: кандидатный набор — authoritative
  Component Registry (одна публичная версия на компонент). Multi-version
  availability model, automatic version discovery или подмена версий требуют
  отдельного архитектурного решения и не вводятся здесь;
* второго источника истины для метаданных компонентов;
* прямого доступа к внутренним БД компонентов;
* internal cross-component imports;
* автоматических интеграций между Business Systems;
* новых сервисов, границ или ownership.

Если для дальнейшего развития Composer потребуется новая архитектура,
изменение контракта, ownership или новое фундаментальное правило, это
оформляется отдельным ADR, а не включается в Slice E.
