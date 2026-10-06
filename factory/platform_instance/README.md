# Platform Instance — Slice F

**Статус:** `IMPLEMENTED` (Slice F, Issue #74)
**Основание:** `docs/adr/ADR-0015-level-2-component-factory-activation.md` §15 (Platform Instance assembly), §11
**Архитектурный закон:** `docs/ARCHITECTURE.md` 1.2.0 §2.2, §16, §20, §21, §31

---

## 1. Назначение

Platform Instance — конкретный продукт заказчика (ARCHITECTURE.md §2.2):

```text
Platform Instance =
    Platform Manifest
  + Configuration
  + Extensions
  + Branding
  + Environment-specific settings
```

Slice F реализует **assembly**: детерминированное, воспроизводимое
представление конкретного Platform Instance из одного принятого/валидного
Platform Manifest (LAW-09). Граница slice'а:

```text
Composer → Platform Manifest → Validated Manifest
    → Slice F — Platform Instance Assembly
    → Concrete Platform Instance (assembly representation)
```

Assembly **фиксирует** состав, а не вычисляет и не разворачивает его. Результат
детерминированно закрепляет:

* identity Platform Instance (`platform_id`);
* точную привязку к Manifest identity/version/digest;
* конкретные component identities/versions — в точности как в манифесте;
* конкретные artifact identities — в точности как в манифесте;
* configuration, extensions, branding — в точности как в манифесте;
* ссылку на Golden Bundle либо явный статус uncertified (§31);
* environment-specific settings — через существующий контрактный путь:
  объявленные компонентом configuration-ключи, переносимые манифестом (§20).
  Отдельный platform-level контракт environment-оверлеев этим slice'ом не
  создаётся;
* детерминированный assembly identity/result (`instance_digest`).

---

## 2. Канонический источник

| Артефакт | Путь | Роль |
|---|---|---|
| Схема | `factory/platform_instance/schema/platform_instance.schema.json` | нормативная JSON Schema (draft 2020-12) |
| Сборка, валидация, digest | `src/platform_instance/` | программный доступ, детерминированная сборка и проверка |
| Тесты | `tests/test_platform_instance.py` | проверка архитектурных правил Slice F |
| CLI | `python -m platform_instance` | validate, digest, assemble |
| Детерминированный пример (текущий) | `factory/platform_instance/example_instance_without_shared_libraries.json` | точная assembly манифеста из `factory/composer/example_request.json` с `platform_id = "example-platform"`; Shared In-process Library не входят в состав (ADR-0021 §2.2, решение D2=B) |
| Предыдущая композиция (сохранена без изменений) | `factory/platform_instance/example_instance.json` | assembly той же композиции до классификации D2=B: 7 членов, собственный `instance_digest`; не редактировался in place — результат классификации зафиксирован как **новый** instance (ADR-0016 §5, §7) |

Конкурирующие представления одной и той же композиции не создаются: у каждой
композиции ровно одно assembly-представление. Разные композиции — разные
Platform Instances, поэтому рядом с текущим примером сохранён предыдущий:
`example_instance.json` (7 членов, композиция до классификации D2=B) и
`example_instance_without_shared_libraries.json` (6 членов, текущий) — два
разных instance с разными `instance_digest`; прежний не редактировался in
place и под текущей классификацией не является допустимым входом. Хранение
конкретных instance-документов определяется потребителем; формат и
валидация — едины.

Текущий пример: `platform_id = "example-platform"`,
`manifest_digest = sha256:3e391dc7a7300cc2b09a803a141b03ca9f2c9930dc3f2b8c50d702eea625471e`,
`instance_digest = sha256:477145e4584b47f250a5181bb37aa21adb54c0a36ec58b3770f11f99e83219e9`.
Прежний instance сохраняет собственные `manifest_digest =
sha256:a5a04028adeae7f7d41978d3bc32dbbf0cf72ceca2350eff7966381b5619c912` и
`instance_digest =
sha256:2be690d9f3c091d23e62b77e32a07c5c0100f98719dedec2f5571498d4508212`
(ADR-0021 §2.2, решение D2=B; ADR-0016 §5, §7).

---

## 3. Формат

```json
{
  "$schema": "factory/platform_instance/schema/platform_instance.schema.json",
  "platform_id": "example-platform",
  "instance_digest": "sha256:...",
  "manifest": {
    "manifest_id": "example-platform",
    "manifest_version": "1.0.0",
    "manifest_digest": "sha256:..."
  },
  "manifest_state": "validated",
  "components": [
    {
      "component_id": "learning",
      "component_version": "0.3.0",
      "artifact": { "artifact_type": "none", "digest": null, "pinned": false }
    }
  ],
  "golden_bundle": null,
  "configuration": { "...": "только если есть в манифесте" },
  "extensions": [ "...только если есть в манифесте" ],
  "branding": { "...": "только если есть в манифесте" }
}
```

Правила:

* `platform_id` — явная identity собираемого Instance, задаётся вызывающей
  стороной; никогда не генерируется и не выводится скрыто.
* `manifest` — точная привязка: `manifest_id + manifest_version +
  manifest_digest` исходного манифеста (LAW-07, LAW-09).
* `manifest_state` — состояние lifecycle исходного манифеста на момент
  assembly; только существующий словарь Slice C (ARCHITECTURE.md §16),
  никакого instance-specific lifecycle не вводится.
* `components` — поэлементная копия состава манифеста (versions, artifacts).
* `golden_bundle` — ссылка из манифеста; `null` — явный статус uncertified
  (ARCHITECTURE.md §31), никогда не «молчаливая» сертификация.
* `configuration`, `extensions`, `branding` — присутствуют только если есть в
  манифесте, и совпадают с ним канонически.

---

## 4. Lifecycle

Slice F не создаёт новых состояний. Instance наследует существующий словарь
манифеста:

```text
draft → validated → approved → published → deployed → superseded → retired
```

Собираются только манифесты в состояниях, прошедших validation:

```text
validated | approved | published | deployed
```

`draft` не прошёл validation; `superseded`/`retired` — исторические состояния
заменённых или отозванных композиций. Сборка из них отвергается
детерминированно.

---

## 5. Determinism / Reproducibility

* Одинаковые authoritative inputs (манифест-документ + `platform_id`) дают
  байт-в-байт одинаковый canonical document и одинаковый `instance_digest`.
* `instance_digest` = `sha256:` от canonical JSON (sort_keys, compact
  separators) всего контента, кроме самого `instance_digest` и `$schema` — та
  же digest-модель, что у catalog `entry_digest`, `manifest_digest` и
  `bundle_digest`.
* Никакого current time, randomness, machine-specific state, network state и
  незафиксированного окружения в identity: документ не содержит
  сгенерированных полей, а timestamp'ы наследуются только из самого входного
  манифеста.
* Floating selectors (`latest`, `current`, `default`, `stable`, `edge`,
  `main`, `master`, `head`, `tip`, `*`) запрещены как identity и как значения
  внутри configuration/branding/extensions.

---

## 6. Validation

`validate_instance_document(instance, manifest)` проверяет — полностью,
детерминированно, sorted:

* структуру против нормативной схемы;
* `instance_digest` против canonical content digest (tamper detection);
* точную привязку к манифесту: identity/version/digest; declared digest
  манифеста равен вычисленному (подмена манифеста отвергается);
* `manifest_state` равен состоянию манифеста и собираем;
* **поверхности применимых factory-контрактов**: состав манифеста повторно
  проверяется существующими authoritative-поверхностями Composer —
  совместимость зависимостей (§18), contract validity, configuration против
  опубликованных configuration schema (§20: неизвестный ключ — BUILD ERROR),
  extensions через опубликованные контракты (§21). Ничего не
  разрешается/подставляется заново: composition view восстанавливается из
  entries манифеста;
* Slice C validation самого манифеста (fail-closed против authoritative
  Component Registry);
* точное сохранение: components, golden_bundle, configuration, extensions,
  branding канонически равны манифесту — расхождение отвергается даже при
  внутренне согласованном `instance_digest`: манифест остаётся единственным
  источником состава;
* согласованность identity: component-scoped configuration, называющая
  обслуживаемый Platform Instance (`platform_id`; у identity —
  `current_platform_id`), должна называть ровно этот Instance.

Golden Bundle evidence при assembly повторно не перепроверяется: манифест
несёт ссылку `bundle_id + bundle_version + bundle_digest` без пути к
документу bundle, и отдельного контрактного способа прочитать bundle по
ссылке не существует. Формат ссылки проверяет Slice C, точное сохранение —
Slice F.

---

## 7. Authoritative inputs

Assembly потребляет только уже определённые входы:

* Platform Manifest (валидный, состояние `validated` и далее) —
  `src/platform_manifest/`;
* Component Registry — через существующие fail-closed загрузчики Slice C/E;
* Configuration / Extensions / Branding / environment-specific settings —
  как content манифеста;
* `platform_id` — явный аргумент assembly.

Новых источников входов, нового artifact storage и нового source of truth не
создаётся. Манифест остаётся единственным источником состава: instance
валидируется против манифеста при каждой проверке.

---

## 8. Чего здесь нет

Slice F реализует только assembly-представление. В этом slice нет:

* deployment engine, Kubernetes, Helm, Terraform, cloud/VM provisioning;
* rollout, canary, traffic switching, rollback orchestration;
* release trains, runtime orchestration, service discovery;
* approval/publication engine — lifecycle манифеста остаётся у Slice C;
* изменения семантики Composer, Registry, Catalog, Manifest, Golden Bundle;
* нового lifecycle vocabulary и нового environment-оверлей контракта
  (platform-level environment settings потребуют отдельного архитектурного
  решения);
* прямого доступа к внутренним БД компонентов;
* internal cross-component imports;
* второго источника истины.

Если для развития Platform Instance потребуется новая архитектура, изменение
контракта, ownership или новое фундаментальное правило, это оформляется
отдельным ADR, а не включается в Slice F.
