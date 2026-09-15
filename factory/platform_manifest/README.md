# Platform Manifest — Slice C

**Статус:** `IMPLEMENTED` (Slice C, Issue #66)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §6, §11, §15
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0 §16

---

## 1. Назначение

Platform Manifest — явный, машинно-читаемый и воспроизводимый артефакт
конкретной платформы (ARCHITECTURE.md §16, ADR-0015 §6).

Manifest фиксирует **конкретную композицию платформы**, а не вычисляет её
скрыто и не выбирает версии автоматически:

* конкретные версии компонентов;
* необходимые сведения об artifact identity / image digests;
* явную identity/version модель манифеста;
* lifecycle и сведения об approval/publication;
* опциональную ссылку на Golden Bundle (без реализации Golden Bundle);
* опциональные configuration, extensions, branding;
* predecessor для воспроизводимой цепочки.

Manifest **не** является второй Registry/Catalog. Он ссылается на
authoritative metadata из `factory/registry/` и `factory/catalog/` и
отвергается валидацией при расхождении.

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Схема | `factory/platform_manifest/schema/platform_manifest.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Загрузка, сборка, валидация | `src/platform_manifest/` | программный доступ, детерминированная сборка и проверка |
| Тесты | `tests/test_platform_manifest.py` | проверка архитектурных правил Slice C |
| CLI | `python -m platform_manifest` | validate, digest, build-example |

Конкурирующие манифесты не создаются в `factory/`. Сам манифест — per-platform
артефакт; его каноническое хранение определяется потребителем, но формат и
валидация — едины.

---

## 3. Формат

Минимальный manifest:

```json
{
  "$schema": "factory/platform_manifest/schema/platform_manifest.schema.json",
  "manifest_id": "education-platform",
  "manifest_version": "1.0.0",
  "manifest_digest": "sha256:...",
  "predecessor": null,
  "lifecycle": { "state": "draft" },
  "components": [
    {
      "component_id": "learning",
      "component_version": "0.3.0",
      "artifact": { "artifact_type": "none", "digest": null, "pinned": false }
    }
  ],
  "golden_bundle": null,
  "validation_attestation": null,
  "approval": null,
  "publication": null
}
```

Полный набор полей:

* `manifest_id` — стабильная идентичность линейки манифестов (lower snake_case / kebab-case).
* `manifest_version` — явная SemVer версия манифеста.
* `manifest_digest` — `sha256:` + 64 hex, digest канонического JSON контента манифеста без самого поля `manifest_digest` и без `$schema`. Вычисляется детерминированно (`sort_keys=True`, компактные разделители).
* `predecessor` — `null` для первого манифеста, либо объект `{ manifest_id, manifest_version, manifest_digest? }`, где `manifest_version` < текущей версии для той же линейки.
* `lifecycle.state` — одно из `draft → validated → approved → published → deployed → superseded → retired`.
* `components` — массив конкретных компонентов, отсортированный по `component_id` для воспроизводимости. Каждый элемент содержит `component_id`, `component_version` (явный SemVer), `artifact` (`artifact_type`, `digest`, `pinned`).
* `golden_bundle` — `null` или ссылка `{ bundle_id, bundle_version, bundle_digest }` (Slice D не реализован, только ссылка).
* `configuration`, `extensions`, `branding` — опциональные, не должны содержать floating selectors.
* `validation_attestation` — опциональная аттестация для standalone профиля.
* `approval` — требуется для `approved` и далее: `{ approved_by, approved_at (ISO8601 UTC), approval_id }`.
* `publication` — требуется для `published` и далее: `{ published_by, published_at, publication_id }`.

---

## 4. Lifecycle

```
draft → validated → approved → published → deployed → superseded → retired
```

Правила:

* `draft` и `validated` не должны содержать `approval` и `publication`.
* `approved` требует `approval`, но `publication` должен быть `null`.
* `published` и далее требуют `approval` и `publication`.
* Переходы разрешены только на следующий шаг (строго линейно) или сохранение того же состояния (idempotent). Пропуск шагов отвергается.
* `published` immutable: нельзя молча переписать тот же `manifest_id + manifest_version` с другим digest/content. Проверяется функцией `check_immutability`.
* `manifest_digest` должен совпадать с вычисленным digest канонического контента.

---

## 5. Authoritative metadata

Manifest ссылается на authoritative sources:

* `factory/registry/component_registry.json` — канонический реестр;
* `factory/catalog/component_catalog.json` — производный discoverable view.

Валидация проверяет:

* каждый `component_id` зарегистрирован в реестре;
* каждый `component_version` совпадает с версией, зарегистрированной в реестре для этого `component_id`;
* нет дубликатов компонентов;
* компоненты отсортированы по `component_id` для детерминизма;
* нет расхождения с authoritative metadata.

Manifest не копирует всю информацию Registry и не создаёт второй источник истины.

---

## 6. Artifact identity / image digests

* `artifact_type`: `none` (нет публикуемого артефакта, Level 0), `container_image`, `source_package`.
* Если `artifact_type == "none"`: `digest == null`, `pinned == false`.
* Если `artifact_type != "none"`: `digest` — обязательный immutable sha256, `pinned == true`.
* Отсутствие обязательного digest приводит к ошибке там, где digest обязателен.
* Floating selectors (`latest`, `current`, `default`, `stable` и т.д.) запрещены как digest.

Manifest только **ссылается/фиксирует identity**, необходимую для воспроизводимой композиции, и не создаёт новую систему хранения артефактов.

---

## 7. Reproducibility

* Одинаковый Manifest + одинаковое authoritative состояние → одинаковый результат проверки.
* Детерминированность: сортировка ключей, сортировка компонентов, отсортированный список ошибок, отсутствие зависимости от порядка обхода файлов, случайных значений, текущего времени, локального окружения, сетевого поиска.
* `manifest_digest` — явный и стабильный.
* Валидация не зависит от `now`.

---

## 8. Validation

```python
from platform_manifest import validate_document, load_manifest, build_manifest_document

errors = validate_document(document, root=root)  # [] когда валиден
manifest = load_manifest(path)                    # загружает и валидирует
doc = build_manifest_document(
    manifest_id="education-platform",
    manifest_version="1.0.0",
    lifecycle_state="draft",
    components=[...],
)
```

CLI:

```bash
python -m platform_manifest validate path/to/manifest.json
python -m platform_manifest digest path/to/manifest.json
python -m platform_manifest build-example
```

---

## 9. Forbidden selectors

Запрещены как production selectors:

```
latest  current  default  stable  edge  main  master  head  tip  *
```

Проверка применяется к полям, которые реально выполняют выбор: `manifest_id`, `manifest_version`, `manifest_digest`, `component_id`, `component_version`, `artifact.digest`, `artifact_type`, `lifecycle.state`, `predecessor`, `golden_bundle`, `extensions`, `configuration`, `branding`.

Слово `latest` остаётся допустимым в prose — в тестах, фикстурах, сообщениях об ошибках и документации, где оно ничего не выбирает.

---

## 10. Чего здесь нет

Slice C реализует только Platform Manifest. В этом slice нет:

* Slice D — Golden Bundles implementation, bundle certification, bundle registry, bundle lifecycle;
* Slice E — Composer, automatic platform composition, automatic dependency resolution, automatic version selection, assembly orchestration;
* deployment / platform provisioning;
* новой системы хранения артефактов;
* прямого доступа к внутренним БД компонентов;
* internal cross-component imports.

Manifest может содержать ссылку на Golden Bundle, но не реализует сам Golden Bundle.
