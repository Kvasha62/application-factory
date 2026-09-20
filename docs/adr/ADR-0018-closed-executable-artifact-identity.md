# ADR-0018 — Closed Executable Artifact Identity

**Статус:** RATIFIED
**Дата:** 2026-09-18
**Ратифицирован:** 2026-09-20 владельцем проекта
**Владелец решения:** Oleg
**Архитектор:** ChatGPT
**Связанные ADR:** ADR-0015, ADR-0016, ADR-0017
**Требует:** ADR-0015, ADR-0016
**Не заменяет:** ADR-0015, ADR-0016, ADR-0017
**Авторитетный архитектурный документ:** `docs/ARCHITECTURE.md`
**Уровень:** Level C
**Область:** Component Registry, Component Factory, Component Artifacts, Platform Manifest, Platform Instance, Deployment & Operations

---

## 1. Context

В архитектуре Platform Factory поставляемый компонент идентифицируется через artifact identity.

Текущая модель Component Registry требует для опубликованного artifact:

* `artifact_type`;
* immutable `digest`;
* `pinned = true`.

Для `artifact_type = none` digest отсутствует.

Эти правила обеспечивают идентичность и pinning artifact, но текущая архитектура не определяет нормативно, **какой набор исполняемых component-owned bytes охватывает artifact identity**.

Проблема становится существенной при наличии native-зависимостей.

Например, один component-owned native library может загрузить другую библиотеку через механизм динамического линкера (`DT_NEEDED`). Такая зависимость может быть исполнена без отдельного Python-level loader event, который Deployment & Operations мог бы использовать как единственный источник enforcement.

Следовательно, одной проверки digest первоначально загруженного native object недостаточно, если архитектура одновременно требует:

> «что исполняется — это verified execution closure».

Без определения artifact boundary Deployment & Operations не может доказательно установить, что транзитивно исполняемый component-owned native dependency принадлежит разрешённому artifact.

Это выявило GAP F-3B:

> **Artifact identity does not currently define a verifiable closed execution unit for native transitive dependencies.**

---

## 2. Problem Statement

Необходимо определить семантику `artifact.digest` таким образом, чтобы:

1. Factory имела единственный источник истины о том, какие component-owned executable bytes принадлежат опубликованному artifact;
2. Platform Manifest и Platform Instance сохраняли эту identity, а не переопределяли её;
3. Deployment & Operations мог fail-closed проверить соответствие фактически исполняемого component content заявленной artifact identity;
4. component-owned native dependencies не могли неявно выйти за пределы verified artifact;
5. D&O не становился новым источником истины для состава artifact;
6. архитектура оставалась technology-neutral на уровне Manifest и Platform Instance;
7. host/runtime dependencies не превращались автоматически в component artifacts.

---

## 3. Decision

### 3.1. Artifact является закрытой исполняемой единицей

Для опубликованного deployable component artifact его `artifact.digest` идентифицирует **closed executable unit**.

Closed executable unit — это неизменяемый набор component-owned executable bytes, который Factory определяет как часть данного опубликованного artifact и который может быть использован при исполнении компонента.

Таким образом:

```text
artifact identity
    =
identity of the closed executable unit
```

а не только identity одного файла, который первым запускается.

Термины canonical artifact unit и sealed artifact unit обозначают один и тот же объект — неизменяемое каноническое artifact representation, над которым Factory вычисляет `artifact.digest`. Closed executable unit — это component-owned executable content, содержащееся в этом unit; отдельной identity он не имеет и идентифицируется тем же `artifact.digest`. Component-owned executable content вне sealed artifact unit не принадлежит closed executable unit.

---

## 4. Семантика `artifact.digest`

`artifact.digest` является immutable identity опубликованного artifact.

Для deployable artifact digest относится к каноническому поставляемому artifact representation.

Digest не означает:

* только digest entrypoint-файла;
* только digest одного `.py` файла;
* только digest одного `.so`;
* только digest одного container entrypoint;
* произвольную runtime-сумму файлов, состав которой определяется D&O.

Digest означает identity **канонического artifact unit**, определённого Factory для соответствующего artifact type.

Изменение component-owned executable content, входящего в sealed artifact unit, должно приводить к изменению artifact identity.

---

## 5. Factory является источником истины

Определение того, **какие component-owned executable bytes являются частью component artifact**, относится к Factory.

Авторитетная цепочка:

```text
Component Registry
        │
        │ authoritative artifact identity
        ▼
Published Component Artifact
        │
        │ immutable digest
        ▼
Platform Manifest
        │
        ▼
Platform Instance
        │
        ▼
Deployment & Operations
```

Factory отвечает за:

* публикацию artifact;
* artifact identity;
* canonical artifact representation;
* digest;
* artifact boundary;
* release/publish lifecycle;
* воспроизводимость artifact.

D&O не имеет права самостоятельно расширять или переопределять artifact boundary.

---

## 6. Artifact boundary не определяется D&O

D&O не должен:

* самостоятельно составлять новый authoritative список native dependencies;
* самостоятельно объявлять `DT_NEEDED` частью Platform Instance;
* изменять artifact identity на основании runtime-наблюдений;
* добавлять найденные runtime libraries в Manifest;
* подменять Factory artifact model собственной моделью composition;
* разрешать дополнительное component-owned executable content только потому, что host dynamic loader способен его найти.

Если проверка конкретного artifact format требует анализа `DT_NEEDED` или другого native metadata, такой анализ является **механизмом проверки уже определённой artifact boundary**, а не механизмом её проектирования D&O.

---

## 7. Component-owned native dependencies

Native executable content, которое принадлежит компоненту и поставляется как часть его deployable artifact, является частью этого artifact.

Например:

```text
component artifact
├── Python code
├── Python extension
├── libcomponent.so
├── libdependency.so
└── other component-owned executable content
```

Если `libcomponent.so` требует `libdependency.so` и обе библиотеки являются component-owned содержимым опубликованного artifact, обе находятся внутри artifact boundary.

D&O должен рассматривать исполнение `libdependency.so` как допустимое только в том случае, если оно покрывается identity и provenance опубликованного artifact.

---

## 8. Host/runtime dependencies

Не вся native-зависимость является частью component artifact.

К runtime environment могут относиться:

* системные библиотеки ОС;
* системный dynamic loader;
* language runtime;
* `libc`;
* `libpython`, если она предоставляется согласованным runtime environment;
* иные host-provided prerequisites.

Такие зависимости не становятся component-owned artifact только потому, что executable object имеет на них ссылку.

Различие:

```text
Component Artifact
        │
        ├── component-owned executable content
        └── component-owned native dependencies

Runtime Environment
        │
        ├── OS
        ├── system libraries
        ├── language runtime
        └── host-provided prerequisites
```

Конкретные правила классификации для каждого artifact type должны быть частью соответствующего artifact metadata/packaging contract.

Термин **artifact contract** в настоящем ADR означает правила конкретного artifact type внутри существующей Factory/Registry модели. Он не вводит новый Component Contract, новый SCS или новую бизнес-границу.

---

## 9. Artifact types

Существующие artifact types:

```text
none
source_package
container_image
```

сохраняются.

### 9.1. `artifact_type = none`

`none` означает отсутствие опубликованного deployable artifact identity.

Это означает, что `none` **не может служить доказательством closed executable artifact**.

Однако это правило **не переопределяет определения `deployed` и `ready`**, установленные ADR-0016 и ADR-0017.

В частности:

> ADR-0018 не переопределяет `deployed` по ADR-0016 §10 и ADR-0017 §33–§35.

First slice может использовать `artifact_type = none` и при этом достичь instance-bound `deployed`, если выполнены соответствующие требования ADR-0016/0017 по identity, version, digest, health и readiness.

Но такой deployment:

* не может заявлять соответствие closed-artifact requirement F-3B;
* не может использовать `artifact_type = none` как доказательство verified closed executable unit;
* не может считать F-3B реализованным только на основании успешного запуска.

Таким образом:

```text
instance-bound deployed
        ≠
F-3B verified closed executable unit
```

### 9.2. `source_package`

`source_package` является immutable artifact unit с определённым содержимым и digest.

Если package содержит executable native content, component-owned native content и соответствующие component-owned dependencies должны быть охвачены artifact boundary.

### 9.3. `container_image`

`container_image` является immutable artifact unit, идентифицируемым digest.

Содержимое sealed image unit входит в artifact identity согласно соответствующему container artifact contract.

Host-provided runtime dependencies вне sealed artifact unit остаются environment concerns.

---

## 10. Manifest

Platform Manifest фиксирует artifact identity, полученную от Factory.

Manifest:

* не создаёт artifact identity;
* не изменяет artifact digest;
* не расширяет artifact boundary;
* не перечисляет runtime-discovered native dependencies;
* не превращает runtime dependency в component artifact.

Manifest является downstream representation уже определённого Factory artifact.

```text
Registry / Factory
       ↓
artifact identity
       ↓
Manifest
```

а не:

```text
Manifest
       ↓
invent artifact boundary
```

---

## 11. Platform Instance

Platform Instance наследует artifact identity из Manifest.

Platform Instance не должен:

* заменять artifact;
* изменять artifact digest;
* добавлять native dependency identity;
* выбирать другой component artifact;
* использовать floating artifact selector.

Platform Instance является детерминированным описанием того, что должно быть запущено, а не местом определения состава component artifact.

---

## 12. Golden Bundle

Golden Bundle продолжает выполнять роль проверки совместимости компонентов, версий и опубликованных artifact identities.

Golden Bundle:

* не становится источником истины native dependency graph;
* не обязан перечислять `DT_NEEDED`;
* не должен дублировать внутреннюю структуру каждого artifact;
* фиксирует совместимость уже идентифицированных artifact units.

Evidence Golden Bundle продолжает быть привязанным к immutable bundle digest.

---

## 13. Deployment & Operations

D&O отвечает за enforcement artifact boundary.

D&O обязан:

1. получить конкретный Platform Instance;
2. проверить его identity и digest;
3. получить соответствующий artifact;
4. проверить artifact identity перед исполнением;
5. не допустить исполнения component-owned executable content вне verified artifact boundary;
6. fail-closed при невозможности доказать соответствие;
7. записывать честное состояние deployment;
8. коррелировать operational evidence с Platform Instance identity/digest.

Область применения:

* пункты 1, 2, 7, 8 применяются к любой deployment operation;
* пункты 3–6 применяются к каждому компоненту, для которого Platform Instance фиксирует published deployable artifact (`artifact_type ≠ none`), независимо от того, требуется ли для capability F-3B verified closed execution. Наличие published artifact — факт Platform Instance, полученный от Factory, а не решение D&O;
* для `artifact_type = none` пункты 3–6 неприменимы, потому что published artifact identity отсутствует (§9.1). Это не разрешение исполнять component-owned executable content: такой компонент исполняется только по правилам ADR-0016/ADR-0017, не может быть частью F-3B verified closed execution, а если capability требует F-3B — операция завершается отказом по §16.

Глубина доказательства по пунктам 4–6:

* соответствие исполняемого artifact опубликованной artifact identity (digest) доказывается всегда; его недоказуемость — отказ всегда (ADR-0016 §7, §8, §10, §20);
* принадлежность каждого исполняемого component-owned executable content closed executable unit доказывается только на основании type-specific artifact contract (§8) и canonical artifact representation (§4, §5); её недоказуемость — отказ для capability, требующей F-3B (§16), а для остальных — запрет заявлять F-3B verified closed execution (§26.7, §27) при честной записи состояния (пункт 7); доказанное исполнение component-owned executable content вне boundary (§15, §17) — отказ всегда.

Требуется ли для capability F-3B verified closed execution, определяется acceptance criteria соответствующего approved work item (ADR-0017 §28, §30; §25 настоящего ADR, шаги 7–10), а не D&O в runtime и не наличием или отсутствием artifact.

D&O не определяет состав artifact.

---

## 14. Execution closure

Конкретный runtime import/load mechanism — в том числе language-level import closure — может использоваться только как implementation mechanism enforcement в пределах authoritative artifact contract.

Он не является источником истины о составе artifact и не является архитектурным определением полного component artifact.

Если конкретный artifact format требует дополнительной проверки native dependency closure, соответствующая проверка должна быть основана на authoritative artifact boundary.

---

## 15. Native dynamic loading

Dynamic loading является частью execution boundary.

### Разрешённая цепочка

```text
verified component artifact
        ↓
component-owned native library
        ↓
component-owned native dependency
```

если весь component-owned executable content покрывается verified artifact identity.

### Запрещённая цепочка

```text
verified component artifact
        ↓
component-owned native library
        ↓
foreign component-owned native library
```

если foreign library не принадлежит verified artifact.

### Environment dependency

```text
component artifact
        ↓
system/runtime library
```

может быть разрешена как dependency согласованного runtime environment, а не как неявное расширение component artifact.

---

## 16. Fail-closed

Если D&O не может установить:

* artifact identity;
* artifact digest;
* provenance executable content;
* принадлежность executable content artifact boundary;
* соответствие runtime artifact опубликованному artifact;

операция должна завершиться отказом для capability, требующей F-3B verified closed execution.

Нельзя заменять отсутствие доказательства предположением:

> «dynamic loader сам знает, где искать библиотеку».

Также нельзя считать:

> «файл находится внутри workspace»

доказательством принадлежности artifact.

---

## 17. Unpinned / unidentified executable content

Запрещено использовать неидентифицированный executable content как замену immutable artifact identity.

В частности, нельзя полагаться на:

* имя файла без artifact identity;
* имя native library без provenance;
* runtime path без verified artifact relationship;
* случайно найденный executable content;
* host search path как источник component identity.

Это отдельное правило provenance и не изменяет определение floating selectors в `ARCHITECTURE.md §1.3`.

---

## 18. Provenance

Каждое component-owned executable content, которое D&O допускает к исполнению, должно иметь проверяемую provenance chain:

```text
Artifact
   ↓
Artifact identity / digest
   ↓
Verified content
   ↓
Runtime execution
```

Отсутствие provenance означает отсутствие основания считать content частью verified component execution closure.

---

## 19. Relationship with ADR-0015

ADR-0015 устанавливает Component Registry как authoritative inventory и включает artifact identity в registry metadata.

ADR-0018 уточняет семантику этой identity:

```text
artifact identity
    =
identity of the closed executable unit
```

ADR-0018 не переносит ownership artifact identity из Factory в D&O.

---

## 20. Relationship with ADR-0016

ADR-0016 остаётся авторитетным для Deployment & Operations.

ADR-0018 уточняет архитектурную семантику artifact identity, необходимую для выполнения требований ADR-0016.

В частности, уточнение применяется к требованиям:

* immutable downstream artifact identity;
* Platform Instance identity;
* verified deployed condition;
* provisioning from instance/config path;
* runtime operations;
* observability;
* fail-closed behavior.

ADR-0018 не ослабляет требования ADR-0016.

F-3B verification не является частью определения `deployed` по ADR-0016 §10 и ADR-0017 §33–§35. Пункт «verified deployed condition» выше относится к определению ADR-0016, а не добавляет F-3B в `deployed`.

---

## 21. Relationship with ADR-0017

ADR-0017 определяет implementation scope Deployment & Operations.

ADR-0018 является архитектурным prerequisite для полного F-3B compliance.

Ратификация ADR-0018:

* не означает, что F-3B уже реализован;
* не означает, что execution closure уже проверяется полностью;
* не авторизует claim of implementation;
* не заменяет approved implementation work item;
* не заменяет independent verification.

First slice ADR-0017 может оставаться valid при `artifact_type = none`, если он выполняет собственные acceptance criteria.

---

## 22. Relationship with `ARCHITECTURE.md`

Настоящий ADR является Level C architectural decision.

До ратификации:

* `docs/ARCHITECTURE.md` остаётся v1.2.0;
* его положения не считаются изменёнными настоящим proposed ADR;
* никаких controlled architecture updates не производится.

После ратификации ADR-0018 должен быть выполнен отдельный controlled update `ARCHITECTURE.md`.

Минимально необходимо рассмотреть уточнение:

* §11 — semantics of artifact digest;
* §16 — artifact/image digest as identity of the sealed artifact unit;
* §31 — platform reproducibility and executable artifact identity;
* §37.1 — Factory ownership of artifact boundary and D&O enforcement.

Этот update не должен входить в тот же change-set, которым ратифицируется ADR, если действующий governance process требует отдельного controlled update.

ADR-0018 не должен становиться конкурирующим источником конституционного текста после ратификации.

---

## 23. Alternatives Considered

### Alternative A — хранить native dependency graph в Platform Instance

Отвергнуто.

Это переносит implementation-specific native composition в desired-state model и нарушает separation:

```text
Factory defines artifact
D&O executes artifact
```

Platform Instance должен идентифицировать artifact, а не заново описывать его внутреннее устройство.

---

### Alternative B — хранить `DT_NEEDED` в Manifest

Отвергнуто.

`DT_NEEDED` является implementation detail конкретного native artifact format.

Его публикация в technology-neutral Manifest:

* связывает Manifest с ELF;
* дублирует artifact internals;
* создаёт второй источник истины;
* не решает проблему других artifact formats.

---

### Alternative C — сделать VEB/runtime loader источником истины

Отвергнуто.

Runtime loader должен **enforce** заранее определённую artifact boundary.

Он не должен решать, какие байты являются частью component artifact.

Иначе D&O фактически становится Factory.

---

### Alternative D — проверять только первый загруженный executable object

Отвергнуто.

Это не гарантирует отсутствие транзитивного исполнения unverified component-owned content.

Именно native transitive loading выявило F-3B.

---

### Alternative E — runtime-discover native dependencies и автоматически добавлять их в trusted closure

Отвергнуто.

Это создаёт динамическую artifact composition:

```text
runtime discovery
    ↓
new trusted content
```

и нарушает immutable/pinned identity.

Runtime discovery может использоваться только для проверки уже определённой artifact boundary.

---

## 24. Consequences

### Positive consequences

После принятия решения:

* artifact digest получает однозначную архитектурную роль;
* Factory остаётся источником истины;
* Manifest и Platform Instance остаются technology-neutral;
* D&O получает чёткую enforcement boundary;
* native transitive dependencies становятся проверяемыми в рамках artifact model;
* `DT_NEEDED` не требуется превращать в Platform Instance metadata;
* runtime environment можно отделить от component-owned artifact;
* F-3B можно реализовывать без архитектурного ownership inversion.

### Constraints

Решение также вводит обязательства:

* Factory должна уметь определить canonical artifact unit;
* deployable artifact должен быть воспроизводимым и immutable;
* artifact packaging должен иметь однозначную boundary;
* существующие artifacts могут потребовать migration;
* `artifact_type = none` не может служить доказательством closed executable artifact;
* D&O implementation должна быть ограничена authoritative artifact contract.

---

## 25. Migration

Migration выполняется отдельно от настоящего ADR и не считается завершённой при одной ратификации.

### CURRENT

```text
Registry
  artifact_type
  digest
  pinned
        │
        ▼
Manifest / Instance
        │
        ▼
D&O
```

Digest гарантирует immutable identity artifact, но closed executable boundary нормативно не определена.

### TARGET

```text
Factory / Registry
        │
        │ defines closed artifact unit
        ▼
Published Artifact
        │
        │ immutable digest
        ▼
Manifest
        │
        ▼
Platform Instance
        │
        ▼
D&O
        │
        └── verifies + enforces boundary
```

Migration должна включать как минимум:

1. определить canonical artifact representation;
2. определить artifact boundary для каждого deployable artifact type;
3. определить distinction между component-owned и runtime/environment content;
4. обеспечить reproducible artifact identity;
5. обновить Registry validation при необходимости;
6. обновить Manifest/Instance contracts при необходимости;
7. реализовать D&O enforcement;
8. добавить F-3B regression tests;
9. провести independent verification;
10. только после acceptance заявить F-3B implemented.

Существующие `artifact_type = none` не получают искусственный digest.

---

## 26. Required implementation tests

После реализации обязательны проверки:

### 26.1. Artifact identity

Одинаковое canonical artifact content даёт одинаковый immutable digest.

Изменение component-owned executable content изменяет artifact identity.

### 26.2. Native transitive dependency

```text
verified loader
    ↓
foreign native dependency
```

должно завершаться отказом, если dependency не принадлежит verified artifact.

### 26.3. Native dependency inside artifact

```text
verified loader
    ↓
verified component-owned dependency
```

должно быть разрешено, если dependency покрывается artifact identity.

### 26.4. Environment dependency

Runtime/system dependency не должна ошибочно классифицироваться как component-owned artifact только из-за факта использования.

### 26.5. Artifact substitution

Замена artifact при сохранении component/platform identity должна завершаться отказом.

### 26.6. Unpinned executable content

Неидентифицированный executable content не должен становиться trusted только вследствие runtime discovery.

### 26.7. Honest state

Невозможность доказать artifact boundary не может использоваться для утверждения F-3B compliance.

Это не отменяет самостоятельных критериев `deployed` по ADR-0016/0017.

---

## 27. Observability

Operational evidence должен позволять установить как минимум:

```text
platform instance identity
platform instance digest
component identity/version
artifact identity/digest
verification result
execution result
failure reason
```

Evidence не должно утверждать F-3B verified closed execution, если artifact boundary не была доказана.

---

## 28. Security boundary

Artifact boundary является security boundary между:

```text
trusted component-owned executable content
```

и:

```text
unverified executable content
```

Наличие физического доступа к файлу, нахождение файла внутри workspace или способность host dynamic loader найти файл не являются доказательством trust.

Trust определяется provenance, artifact identity и verification.

---

## 29. Non-goals

Настоящий ADR не:

* вводит новый microservice;
* создаёт новый SCS;
* вводит новый Component Contract;
* требует конкретного container runtime;
* требует конкретного ELF parser;
* требует конкретного Python import mechanism;
* требует хранения `DT_NEEDED` в Manifest;
* требует хранения native dependency graph в Platform Instance;
* меняет business data ownership;
* меняет tenant ownership;
* разрешает D&O изменять Platform Manifest;
* разрешает floating artifact selectors;
* утверждает, что текущая F-3B implementation уже соответствует решению;
* переопределяет `deployed` из ADR-0016/ADR-0017.

---

## 30. Required controlled update after ratification

После ратификации необходимо выполнить отдельный controlled update `docs/ARCHITECTURE.md`.

Минимальная область проверки:

### §11 — Component Version и идентификаторы

Уточнить, что digest опубликованного deployable artifact идентифицирует sealed/closed executable artifact unit, а не только произвольный entrypoint file.

### §16 — Platform Manifest

Уточнить, что artifact/image digest в Manifest является identity соответствующего sealed artifact unit.

### §31 — Definition of Done для Platform

Уточнить, что reproducibility включает воспроизводимую artifact boundary для deployable components.

### §37.1 — Factory / Deployment & Operations

Уточнить разделение:

```text
Factory:
  defines/publishes artifact boundary

D&O:
  verifies/enforces artifact boundary
```

Controlled update должен быть отдельным governance action и не должен использоваться для скрытого изменения других архитектурных правил.

---

## 31. Acceptance Criteria for Ratification

ADR-0018 готов к ратификации только если независимое ревью подтверждает:

* нет противоречия `docs/ARCHITECTURE.md`;
* нет противоречия ADR-0015;
* нет противоречия ADR-0016;
* нет противоречия ADR-0017;
* Factory остаётся Source of Truth для artifact identity;
* D&O не становится владельцем artifact composition;
* native transitive dependencies покрываются artifact boundary model;
* runtime/environment dependencies отличимы от component-owned dependencies;
* `artifact_type = none` не выдаётся за verified closed artifact;
* `deployed` ADR-0016/0017 не переопределяется;
* type-specific artifact contracts (§8) и canonical artifact representation (§4, §5) являются последующими Factory/Registry contracts (§25, шаги 1–4): ADR-0018 фиксирует их ownership (Factory) и обязательность для F-3B, но не их конкретную форму; они не передают ownership artifact identity и artifact boundary в D&O;
* после ратификации существует понятный controlled update `ARCHITECTURE.md`;
* migration CURRENT → TARGET определена;
* rejected alternatives зафиксированы.

---

## 32. Status

**RATIFIED — ратифицирован владельцем проекта 2026-09-20.**

Как было зафиксировано в статусе PROPOSED, до ратификации:

* настоящий ADR не является законом проекта;
* `docs/ARCHITECTURE.md` не изменяется;
* F-3B остаётся архитектурным GAP;
* код не должен изменяться для «соответствия» этому draft;
* implementation не может заявлять compliance с ADR-0018.

Ратификация выполнена владельцем проекта после независимого архитектурного ревью (§31; ревью завершено без блокирующих замечаний) в соответствии с действующим governance process и подтверждает решение в том виде, в каком оно записано, как Level C architectural decision (`docs/ARCHITECTURE.md` §34.1).

Зафиксированное состояние после ратификации:

* `docs/ARCHITECTURE.md` этой ратификацией не изменяется и остаётся v1.2.0 RATIFIED; controlled update §11, §16, §31, §37.1 выполняется отдельным governance action (§22, §30);
* ADR-0015, ADR-0016 и ADR-0017 не изменяются; `deployed` по ADR-0016 §10 и ADR-0017 §33–§35 не переопределяется (§9.1, §20);
* F-3B остаётся архитектурным GAP: ратификация не реализует F-3B, не авторизует implementation и не изменяет код (§21, §25);
* implementation не может заявлять compliance с ADR-0018 без approved work item и независимой verification/acceptance (§21, §26, §27; ADR-0017 §28, §30);
* настоящий ADR входит в confirmed list `docs/adr/README.md` (ADR-0016 §31).

Ратификация не означает, что F-3B реализован.

Реализация F-3B требует отдельного approved work item и независимой verification/acceptance.

**ADR-0018 status: RATIFIED.**
