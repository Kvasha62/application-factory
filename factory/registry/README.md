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


## 9.1 Canonical representation and artifact boundary — F-3B Deliverables 1–2 (Issue #84) — NORMATIVE

Per ADR-0018 §3.1,§4,§5,§8,§13,§14,§16,§18,§21,§25-27,§31-32, ARCHITECTURE.md §11,§16,§31,§37.1. Это нормативный Factory/Registry contract как deliverable Issue #84, не изменение архитектуры и не implementation. `source_package` и `container_image` сейчас не публикуются (все entries `none` per §9) — контракт определяет как Factory MUST определять canonical форму когда они будут публиковаться. Выбран Variant B: Registry хранит identity + immutable reference на canonical manifest `factory/artifacts/<digest>/canonical.json`, content-addressed.

### 9.1.1 General principles

- canonical representation — immutable, deterministic, technology-neutral описание sealed artifact unit, определённое Factory.
- sealed artifact unit — весь canonical representation как опубликован, identity = `artifact.digest` per ADR-0018 §3.1,§4.
- `artifact.digest = SHA256(canonical.json bytes)` где bytes — UTF-8, sorted keys, no whitespace per §9.1.4.
- `content_digest = SHA256(content bytes)` per file — `sha256:<hex>`.
- Factory — единственный source of truth для canonical, boundary, digest, reproducibility — §5, §37.1.
- Manifest/Instance наследуют identity, не создают и не расширяют boundary, не хранят native graph / DT_NEEDED — §10,§11, Alt A/B rejected §23.
- D&O получает authoritative contract, проверяет, fail-closed, не создаёт boundary, не добавляет runtime-discovered — §6,§14,§16.
- Physical presence файла внутри canonical representation is NOT sufficient proof of ownership; only content Factory defines as part of closed executable unit enters boundary.
- Non-owned content внутри sealed artifact allowed only if non-executable.
- Executable content внутри sealed artifact MUST BE component-owned per Factory contract; Factory MUST NOT define executable as non-owned; if ownership cannot be proven — fail-closed per §16.
- `canonical.json` — derived immutable metadata, NOT part of sealed artifact entries. Явно запрещено включать путь `canonical.json` в entries. Устраняет circularity `digest → manifest → digest`.
- Workspace, host search path, runtime-discovered files — не доказательство trust — §16,§17,§28.
- `artifact_type=none` — digest null, no artificial digest, не часть F-3B, при требовании F-3B → отказ per §16 — §9.1.
- `deployed` = ADR-0016 §10 + ADR-0017 §33-35, не изменяется — §9.1,§20,§32.
- Runtime discovery не расширяет trusted closure, DT_NEEDED может использоваться как enforcement mechanism, но не как source of truth — §6.

### 9.1.2 source_package — canonical form `source_package/v1`

**Sealed unit**: deterministic file set, опубликованный Factory. Физический носитель (directory/tar/zip) — implementation detail; нормативная форма — content manifest.

**Paths — normative**:

- Encoding UTF-8, Unicode NFC, case-sensitive byte compare.
- Path MUST already be Unicode NFC (Normalization Form C). Non-NFC path MUST be rejected — verifier MUST check NFC via Unicode NFC check, reject if not NFC.
- Implicit normalization during verification/canonicalization is forbidden — canonicalization MUST NOT normalize non-NFC to NFC, MUST reject.
- Separator `/` only, reject `\`.
- Reject empty path, absolute path (`/…`), path component `.` or `..`, `//`, `./`, `../`, paths containing null byte.
- Reject duplicate paths — duplicate → validation error.
- Directory/file collision: если `a/b` существует как file, `a/b/c` не должен существовать — collision → error.
- Reserved: reject `.` и `..` как whole path.
- Ordering: lexicographic UTF-8 byte order by `path`.

**Files**:

- empty files: `content_digest = sha256("")` deterministic.
- binary files: `content_digest = sha256(bytes)`.
- permissions: только executable bits влияют на executable semantics. Остальные (uid/gid, timestamps, atime, owner bits не влияющие на исполнение) NOT in digest input.
- hardlink: представлен как два file entries с одинаковым `content_digest` но разными path — valid, no special hardlink type.

**Directories**:

- Explicit dir entries required для всех директорий включая empty и всех parents. Empty dir → dir entry без children.
- Nested: каждый parent должен иметь explicit dir entry.

**Symlinks**:

- Type `symlink`, поле `symlink` = target string, `content_digest` null.
- Target MUST be POSIX relative only, reject absolute.
- Target MUST stay inside root: resolving target against symlink parent must not escape via `..` beyond root — escaping → reject.
- Target MUST exist as entry in manifest (file/dir/symlink) — no dangling → reject.
- Cycle detection via DFS — cycle → reject.
- Symlink to directory allowed, target must be dir entry.
- Symlink to executable: symlink itself `executable=false`, но target must be owned executable если symlink используется для execution. Symlink сам не входит в boundary.
- Symlink escaping root → reject.

**content_digest**: `sha256:<hex>` где hex = `sha256(file bytes)`.

**executable — Factory declarative, not D&O**:

- Поле `executable: bool` в entry — декларативное свойство Factory.
- Factory MUST выставить `executable=true` для любого файла который может исполняться:
  - mode & 0o111 !=0, OR
  - ELF magic `\x7fELF` в начале, OR
  - PE magic `MZ` в начале, OR
  - Mach-O magic `0xFEEDFACF`, `0xFEEDFACE`, `0xCAFEBABE`, OR
  - shebang `#!` в начале, OR
  - suffix в Factory-defined executable set: `[".py",".pyc",".pyo",".pyd",".so",".dll",".dylib",".exe"]` — РОВНО этот список, пустая строка `""` запрещена. Extensionless executable определяется только через `+x` / magic / shebang, а не через пустой suffix.
- Если физический файл имеет любой из выше физических признаков, но manifest имеет `executable=false` → manifest invalid (hiding executable) → fail-closed.
- Factory MAY пометить non-executable файл как `executable=true` как conservative (treat data as executable) — allowed, но тогда `owned` MUST be true.
- Extensionless executable допустим через `+x`.
- Scripts и native binaries покрыты magic + suffix + `+x`.

Важно: `EXECUTABLE_CONTENT_SUFFIXES` из `src/deployment_operations/runtime_worker.py` НЕ является source of truth. Factory Contract содержит собственный нормативный список выше. D&O константа остаётся defensive audit mechanism.

**owned**:

- `owned: bool` — Factory declaration, true = component-owned.
- Rule: если `executable==true` то `owned MUST be true`, иначе artifact invalid — fail-closed. Non-executable `owned==false` allowed (data, docs).
- Hardlink с одинаковым digest: каждый path имеет свой owned flag, должен соответствовать executable rule.

**Canonical JSON serialization**:

- `canonical.json = {"form":"source_package/v1","entries":[...sorted...]}` UTF-8, keys sorted lexicographic, no whitespace: `json.dumps(..., sort_keys=True, separators=(',',':'), ensure_ascii=False).encode('utf-8')`.
- Entries sorted lexicographic by path.
- `form` = `source_package/v1`.

**Digest**:

- `content_digest = "sha256:"+hex(sha256(file bytes))`
- `artifact.digest = "sha256:"+hex(sha256(canonical.json bytes))`
- `canonical.json` NOT part of entries — устраняет circularity.

### 9.1.3 container_image — canonical form `container_image/v1`

**Sealed unit**: immutable image artifact = ordered layers + config. Физический carrier OCI layout — implementation detail; нормативная форма — canonical.json с layers.

**Layers**:

- `layers: [{digest, ownership, entries}, ...]` ordered `0..N-1`, layer 0 base.
- `ownership: "base"|"component"` per layer — machine-readable механизм для base/runtime vs component-owned distinction.
- Каждый layer's `entries` — те же правила что для source_package (paths, symlink, etc.) плюс whiteout handling.
- Duplicate paths across layers: later layer заменяет earlier file/dir/symlink с тем же path.
- Replacement semantics:
  - file→file, dir→dir, symlink→symlink: later wins
  - file→dir: earlier file deleted, dir created
  - dir→file: earlier dir и его children deleted, file created
  - symlink replacement: later symlink replaces file/dir
- Deletion/whiteout: файл `.wh.<name>` внутри dir → entry type `whiteout` с target path `<dir>/<name>` → удаляет file/dir с этим именем из lower layers. Whiteout file сам не в final view.
- Opaque directory: файл `.wh..wh..opq` внутри dir → dir opaque → все lower layer entries под этим dir скрыты.
- Ordering preserved, final filesystem = результат применения layers последовательно с правилами выше детерминирован.

**Layer digest — normative (container_image/v1)**:

- `layers[].digest` — identity конкретного canonical layer, distinct от `artifact.digest`.
- Формула: `layer_canonical_i = {"ownership": <ownership>, "entries": [...sorted...]}` где `entries` — все entries этого layer включая file/dir/symlink/whiteout/opaque в lexicographic order по `path`, с теми же правилами что source_package.
- Canonical serialization для layer: UTF-8, sorted keys, no whitespace: `json.dumps(layer_canonical_i, sort_keys=True, separators=(',',':'), ensure_ascii=False).encode('utf-8')`, deterministic ordering, entries sorted.
- `layer.digest = "sha256:"+hex(sha256(layer_canonical_i bytes))`.
- В `layer_canonical_i` НЕ входит поле `digest` самого layer — no circularity. Входят только `ownership` и `entries`.
- `artifact.digest` — отдельный: SHA256 топ-level `canonical.json` bytes, который содержит уже вычисленные `layers[].digest` как часть структуры.
- Ясно: `layer.digest` = digest canonical layer, `artifact.digest` = digest canonical image (form+layers+config).

**Full canonical JSON structure — normative (container_image/v1)**:

- Структура:
```json
{
  "form": "container_image/v1",
  "layers": [
    {"digest": "sha256:<hex>", "ownership": "base|component", "entries": [ ... ]},
    ...
  ],
  "config": {
    "entrypoint": [...],
    "cmd": [...],
    "working_dir": "..."
  }
}
```
- `form` MUST be `"container_image/v1"`.
- `layers` ordered `0..N-1`, каждый с `digest`, `ownership`, `entries` sorted lexicographic.
- `config` содержит только identity fields: `entrypoint[]`, `cmd[]`, `working_dir`. Non-identity fields (`env`, `labels`, `annotations`, `exposed_ports`, `volumes`, `user`, `architecture/platform`) NOT in canonical JSON — не входят в digest.
- Canonical serialization для всего image: UTF-8, sorted keys at all levels, no whitespace: `json.dumps(canonical, sort_keys=True, separators=(',',':'), ensure_ascii=False).encode('utf-8')`, deterministic ordering.
- `artifact.digest = "sha256:"+hex(sha256(canonical.json bytes))` где bytes — сериализация выше.
- Что входит в `artifact.digest`: `form`, `layers` (включая каждый `digest`, `ownership`, `entries` с их `content_digest`, `executable`, `owned`, `symlink`, `type`), `config` identity fields. Не входит: non-identity config, timestamps, uid/gid, atime.

**Whiteout / opaque canonical representation — normative**:

- Whiteout entry — canonical representation для удаления lower layer entry:
  - `path`: `"<dir>/.wh.<name>"` — путь самого whiteout marker файла внутри layer
  - `type`: `"whiteout"` — MUST
  - `target`: `"<dir>/<name>"` — full path удаляемого file/dir из lower layers — MUST
  - `executable`: `false` — MUST
  - `owned`: `false` — MUST
  - `content_digest`: `null` — MUST
  - `symlink`: `null` — MUST
  - `content_digest` для whiteout — null, т.к. нет content. Whiteout entry входит в layer entries и влияет на `layer.digest`, но сам не в final view.
- Opaque-directory entry — canonical representation для opaque dir:
  - `path`: `"<dir>/.wh..wh..opq"` — путь marker
  - `type`: `"opaque"` — MUST
  - `executable`: `false` — MUST
  - `owned`: `false` — MUST
  - `content_digest`: `null` — MUST
  - `symlink`: `null` — MUST
  - Указывает что dir `<dir>` opaque — все lower layer entries под `<dir>` скрыты.
- Mandatory fields и правила:
  - Для `whiteout` и `opaque`: MUST have `path`, `type`, `executable=false`, `owned=false`, `content_digest=null`, `symlink=null`. Для whiteout дополнительно MUST have `target`.
  - `executable` и `owned` для whiteout/opaque всегда false — они не executable content, не owned.
  - `content_digest` всегда null — нет file bytes.
  - Canonicalization: whiteout/opaque entries сортируются lexicographic по `path` вместе с другими entries, участвуют в layer canonical serialization без ambiguity.
  - Final filesystem: whiteout и opaque marker файлы сами не входят в final view; whiteout удаляет target, opaque скрывает lower entries.
  - Digest: whiteout/opaque не имеют content_digest, но их присутствие (path, type, target) входит в `layer_canonical_i` и тем самым в `layer.digest` и `artifact.digest`.


**Final filesystem**: после применения всех layers результат должен удовлетворять тем же path правилам что source_package (no duplicate, no collision).

**Executable handling**:

- Для layer с `ownership=base`: executable файлы MAY иметь `owned=false` (environment/runtime, e.g., `/bin/sh`, `libc.so`) — allowed, они NOT part of component boundary.
- Для layer с `ownership=component`: любая entry с `executable==true` MUST иметь `owned==true`, иначе artifact invalid.
- Boundary = `{e.path | e in final view && e.executable && e.owned && layer ownership=component}` — component-owned executable bytes.

**Config**:

- Identity config fields (enter digest): `entrypoint[]`, `cmd[]`, `working_dir`
- Non-identity fields (NOT enter digest): `env`, `labels`, `annotations`, `exposed_ports`, `volumes`, `user`, `architecture/platform`
- Явный список, без расплывчатого "executable semantics". Config хранится в canonical.json `config` объекте.

**Provenance**: как для source_package, плюс layer ownership.

### 9.1.4 Canonical manifest storage

- Immutable derived metadata at `factory/artifacts/<artifact.digest>/canonical.json` где `<artifact.digest>` = `sha256:<64 lowercase hex>` — нормативная reference string `factory/artifacts/sha256:<64 lowercase hex>/canonical.json`. Форма `factory/artifacts/<hex>/canonical.json` без префикса `sha256:` запрещена.
- `artifact.digest = sha256:<64 lowercase hex>` = `SHA256(canonical.json bytes)` в формате `sha256:<hex>` — no circularity т.к. canonical.json не содержит свой digest и не содержит свой путь.
- `content_digest = SHA256(content bytes)` per file в формате `sha256:<hex>`.
- Явно запрещено включение `canonical.json` пути в entries sealed artifact.

### 9.1.5 Physical artifact ↔ canonical verification

Verifier (независимый, без D&O кода) MUST:

- enumerate physical sealed artifact: для source_package — walk directory / untar, для container_image — enumerate layers in order
- для каждого файла вычислить `content_digest = SHA256(file bytes)`, для symlink — проверить target, для dir — проверить explicit entry
- построить actual representation per §9.1.2/§9.1.3, детерминированно canonicalize (NFC, sorted keys, separators)
- вычислить `SHA256(canonical.json bytes)` и сравнить с `artifact.digest`
- обнаружить и fail-closed:
  - extra files — файл в physical, отсутствует в canonical
  - missing files — файл в canonical, отсутствует в physical
  - changed bytes — `content_digest` mismatch
  - changed paths — path rename → extra+missing
  - changed symlinks — target string mismatch или escaping
  - changed executable semantics — mode/magic/shebang/suffix mismatch с `executable` flag
  - changed ownership declaration — `owned` flag mismatch
  - changed layer contents — layer digest mismatch, whiteout/opaque нарушение

### 9.1.6 Registry — Variant B

Registry stores (source of truth):

- `artifact_type`
- `digest`
- `pinned`
- `canonical_form`

Canonical manifest stored at immutable content-addressed path:

```text
factory/artifacts/<artifact.digest>/canonical.json
```
где `<artifact.digest>` = `sha256:<64 lowercase hex>`, reference string `factory/artifacts/sha256:<64 lowercase hex>/canonical.json`. Форма `factory/artifacts/<hex>/canonical.json` без префикса `sha256:` запрещена.

Publication order — normative:

```text
build physical sealed artifact
  → compute entries per §9.1.2/§9.1.3
  → write canonical.json per §9.1.2 serialization
  → hash canonical.json → artifact.digest
  → store canonical.json at factory/artifacts/<digest>/canonical.json
  → publish Registry identity (type, digest, pinned, canonical_form, canonical_manifest)
```

Atomicity: Registry entry без существующего canonical.json → invalid. Tamper detection: `artifact.digest` MUST equal `SHA256(canonical.json bytes)`, иначе fail.

### 9.1.7 Ownership — base|component

- Layer `ownership: base|component` per layer — machine-readable.
- `base` layer: executable files MAY be `owned=false` (environment/runtime e.g., `/bin/sh`, `libc.so`, `libpython`) — allowed, NOT part of component boundary. Это legitimate base/runtime executable content, не становится component-owned автоматически.
- `component` layer: любой entry с `executable==true` MUST `owned==true`, иначе artifact invalid.
- External/base content не может стать component-owned лишь потому что используется — MUST быть скопирован/vendored в component layer и Factory MUST аттестовать что он принадлежит component layer.
- Copied/vendored content MAY стать component-owned только когда Factory аттестует что оно принадлежит component layer (same content_digest, но path в component layer с owned=true).
- Final ownership path определяется topmost layer предоставляющим этот path. Если topmost — component layer owned=true → final owned=true. Если topmost — base layer owned=false → final owned=false.
- Boundary = `{final view path | executable && owned && layer ownership=component}`.

### 9.1.8 Provenance chain — normative

```text
Factory declaration (entries with path, content_digest, executable, owned, symlink, ownership)
  → content_digest = SHA256(content bytes)
  → canonical.json (sorted entries, sorted keys, no whitespace)
  → artifact.digest = SHA256(canonical.json bytes)
  → Registry identity (artifact_type, digest, pinned, canonical_form, canonical_manifest)
  → Golden Bundle (совместимость digest, не хранит native graph)
  → Manifest (inherits artifact_type, digest, canonical_form)
  → Instance (inherits same)
  → D&O verification (recompute canonical, check digest, check executable=>owned, check physical==canonical)
  → Worker verified execution (executes only verified content)
```

### 9.1.9 Manifest / Instance inheritance

Inherit ONLY:

```text
artifact_type
digest
canonical_form
```

Do NOT become source of truth для:

- artifact boundary
- DT_NEEDED
- import graph
- workspace files
- runtime-discovered files
- native dependency graph

D&O получает authoritative canonical manifest по identity Instance через `factory/artifacts/<digest>/canonical.json` используя digest из Instance + Registry.

### 9.1.10 D&O boundary enforcement

```text
Factory defines
  ↓
Registry authenticates
  ↓
Manifest/Instance inherit
  ↓
D&O verifies + enforces
  ↓
Worker executes only verified content
```

D&O:

- obtains authoritative boundary из Factory canonical manifest используя digest из Instance
- verifies artifact против canonical manifest (digest, boundary, ownership, physical binding)
- executes only verified content
- MUST NOT derive authoritative boundary из runtime discovery или suffix heuristics

Существующие проверки:

- `runtime.py::_module_candidates()` — сейчас implicit contract (glob .so) — MUST быть изменён на enforcement против content_manifest, а не определение boundary
- `runtime_worker.py::_resolve_under()` — аналогично MUST быть изменён
- `EXECUTABLE_CONTENT_SUFFIXES` — остаётся как defensive/audit mechanism (что считается executable для audit), но НЕ как source of truth для Factory boundary composition

### 9.1.11 artifact_type=none — normative

- `artifact_type=none`
- `digest = null`
- `pinned = false`
- `canonical_form = null`
- `canonical_manifest = null`
- No artificial digest — запрещено создавать digest для none
- Not a verified closed artifact — не может быть частью F-3B verified closed execution
- If capability explicitly requires F-3B → execution MUST fail-closed per ADR-0018 §16
- Ordinary execution semantics remain governed by ADR-0016/ADR-0017

### 9.1.12 Registry schema fields

- `canonical_form`: `null` для none, `"source_package/v1"` для source_package, `"container_image/v1"` для container_image
- `canonical_manifest`: `null` для none, string reference `factory/artifacts/sha256:<hex>/canonical.json` для deployable types
- Semantic rules (enforced в Factory validation, не только JSON Schema):
  - `artifact_type=none → digest null, pinned false, canonical_form null, canonical_manifest null, no artificial digest`
  - `source_package → digest string, pinned true, canonical_form="source_package/v1", canonical_manifest string`
  - `container_image → digest string, pinned true, canonical_form="container_image/v1", canonical_manifest string`
  - `executable=>owned` для component layers (base layers MAY executable owned=false)
  - No executable non-owned в component layers
  - Paths NFC, `/` only, no duplicate, no collision, symlink inside root, no dangling, no cycle
  - canonical.json NOT in entries, no circularity

### 9.1.13 Cross-type invariants (preserved)

- `artifact.digest` = identity sealed artifact unit, не identity execution graph — §4
- Factory — единственный source of truth для canonical, boundary, digest, reproducibility — §5, §37.1
- Manifest/Instance наследуют identity, не создают и не расширяют boundary, не хранят native graph / DT_NEEDED — §10,§11, Alt A/B rejected §23
- D&O получает authoritative contract, проверяет, fail-closed, не создаёт boundary, не добавляет runtime-discovered — §6,§14,§16
- Workspace, host search path, runtime-discovered files — не доказательство trust — §16,§17,§28
- `artifact_type=none` — digest null, no artificial digest, не часть F-3B, при требовании F-3B → отказ per §16 — §9.1
- `deployed` = ADR-0016 §10 + ADR-0017 §33-35, не изменяется — §9.1,§20,§32
- Runtime discovery не расширяет trusted closure, DT_NEEDED может использоваться как enforcement mechanism, но не как source of truth — §6

### 9.1.14 What this contract does NOT prescribe

- конкретный архивный формат (tar/zip/directory) — implementation detail, canonical — JSON manifest
- конкретный OCI/runtime implementation — OCI может быть реализацией, но не архитектурным требованием
- ELF parser как обязательная архитектура — только magic detection для валидации, не mandatory parser
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
