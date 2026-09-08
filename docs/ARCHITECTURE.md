# ARCHITECTURE.md

# Фундаментальная архитектура «Фабрики сборных программных продуктов»

**Версия:** 1.2.0
**Статус:** RATIFIED — технический закон проекта
**Дата:** 2026-09-08
**Основание изменения:** ADR-0010
**Область действия:** вся дальнейшая разработка Platform Factory, Component Catalog, компонентов, Golden Bundles и Platform Instances.

---

## 0. Назначение документа

Этот документ является техническим законом проекта. Он определяет архитектурные границы, владение данными, контракты, версионирование, миграции, сборку платформ, обновление, откат, кастомизацию, уровни зрелости и обязательства разработчиков и AI-инструментов.

### 0.1. Главный принцип

Мы строим не одну монолитную программу и не набор случайных микросервисов. Мы строим фабрику повторно используемых программных компонентов, из которых можно воспроизводимо собирать различные самостоятельные программные продукты.

```
                PLATFORM FACTORY
                       |
        +--------------+--------------+
        |              |              |
        v              v              v
 COMPONENT CATALOG  GOLDEN BUNDLES  COMPOSER
        |              |              |
        +--------------+--------------+
                       |
                       v
              PLATFORM MANIFEST
                       |
                       v
              PLATFORM INSTANCE
```

### 0.2. Соотношение документов проекта

`ARCHITECTURE.md` — единственный действующий архитектурный закон. `docs/adr/` — единственный каталог ADR и архитектурной истории.

Детализирующие документы создаются только при реальной необходимости и не получают нормативного приоритета над ARCHITECTURE.md. Отсутствующий исторический документ не реконструируется предположением. Ссылка на документ сама по себе не является доказательством его существования или выполнения описанной в нём работы.

| Слой | Канонический источник | Роль |
|---|---|---|
| Закон | `docs/ARCHITECTURE.md` | обязательные архитектурные правила |
| Решения | `docs/adr/` | история изменений и решений |
| Реализация | код и контракты соответствующего компонента | исполнение закона |
| Операционная политика | отдельный документ при необходимости | конкретные операционные процедуры |

При конфликте приоритет у ARCHITECTURE.md. Существенные изменения приводятся к нему через ADR.

---

## 1. Архитектурная конституция

Следующие правила являются обязательными архитектурными ограничениями.

### 1.1. Нельзя нарушать границы владения

Компонент не имеет права напрямую читать или изменять внутреннюю БД другого компонента.

Разрешено взаимодействие только через опубликованные контракты: API; события; Data Export / CDC; официальные интеграционные механизмы.

Запрещено: SQL в чужую БД; импорт внутренних модулей другого компонента; обращение к внутренним таблицам; использование внутренних очередей; зависимость от неописанных внутренних структур.

### 1.2. Нельзя создавать Customer Fork без крайней необходимости

Порядок кастомизации:

```
Configuration → Extension → Plugin / Custom Component →
изменение самого компонента → Fork — только как исключение
```

Fork считается архитектурным долгом и требует отдельного решения.

### 1.3. Нельзя использовать latest как архитектурную зависимость

Платформа всегда собирается из конкретных версий компонентов. Продакшен-образы фиксируются по digest.

### 1.4. Нельзя делать breaking change незаметно

Breaking change требует новой major-версии либо явно утверждённой стратегии совместимости, миграционного плана, проверки контрактов и плана перехода потребителей.

### 1.5. Нельзя откатывать production database разрушительно

Production migrations — forward-only. Rollback выполняется откатом программного артефакта/манифеста, а не удалением уже применённой схемы и данных.

### 1.6. Нельзя вводить микросервис только ради «красивой архитектуры»

Microservice — внутренняя единица деплоя. Границы продукта определяются бизнес-возможностями, а не количеством процессов.

---

## 2. Иерархия понятий

```
PLATFORM FACTORY
│
├── COMPONENT CATALOG
│   └── COMPONENT
├── GOLDEN BUNDLES
├── COMPOSER
├── PLATFORM MANIFEST
└── PLATFORM INSTANCE
    ├── Tenant
    ├── Configuration
    ├── Extensions
    └── Branding
```

### 2.1. Platform Factory

Фабрика — система разработки, регистрации, проверки, сборки, версионирования и поставки компонентов. Фабрика не является конкретным продуктом заказчика.

### 2.2. Platform Instance

Platform Instance — конкретный развёрнутый продукт заказчика.

```
Platform Instance =
    Platform Manifest
  + Configuration
  + Extensions
  + Branding
  + Environment-specific settings
```

### 2.3. Tenant

Tenant — конечный клиент Platform Instance, например школа или магазин. Tenant принадлежит конкретной Platform Instance и имеет lifecycle:

`provisioning → active → suspended → deletion_requested → deleted`.

Tenant Authority владеет реестром и lifecycle tenant. Суб-арендатор является внутренним понятием компонента, если он нужен его бизнес-модели, но не становится глобальной сущностью платформы без отдельного архитектурного решения.

### 2.4. Component

Component — основная единица каталога и поставки. Каждый компонент имеет владельца, внешний контрактный контур, lifecycle и публичную SemVer-версию.

Минимальный паспорт компонента включает код, публичные контракты, data ownership, migrations, events, tests и documentation.

### 2.5. Microservice

Microservice — внутренняя единица реализации и деплоя компонента. Микросервис не становится самостоятельным компонентом только потому, что является отдельным процессом.

Если внутренняя часть требует собственного публичного контракта, самостоятельного lifecycle, release cycle и независимого использования, она может быть выделена в отдельный Component.

---

## 3. Классы компонентов

| Класс | UI | Собственные данные | Примеры |
|---|---|---|---|
| **A. Business System / SCS** | Да | Да | Learning, Commerce, Booking |
| **B. Platform Service** | Нет / служебный UI | Да | Identity, Payments, Media, Notifications |
| **C. Computational Service** | Нет | Опционально | Pricing Engine, Route Optimizer |
| **D. Streaming Pipeline** | Нет | Собственный state | Telemetry Ingest, Analytics Pipeline |

Business System — самостоятельная бизнес-система. Platform Service — общий сервис. Computational Service — вычислительная способность, обычно stateless. Streaming Pipeline — высокочастотный потоковый компонент со своим state при необходимости.

---

## 4. Уровни архитектурной зрелости

### Level 0 — Modular Monolith

Физически может использоваться одна PostgreSQL. Границы владения данными сохраняются логически.

### Level 1 — Independent Applications

Компоненты выделяются при наличии реальной причины: две и более команды; независимый release cycle; другой профиль нагрузки; необходимость независимого масштабирования; самостоятельная поставка.

### Level 2 — Component Factory

Появляются Component Catalog, Component Registry, Golden Bundles, Composer, Platform Manifest и независимая поставка компонентов.

### Level 3 — Internal Microservices

Крупный компонент может быть дополнительно декомпозирован. Это оптимизация реализации, а не обязательная цель. Обратная консолидация разрешена.

### 4.1. Гейты фабричного режима

Манифест платформы обязателен всегда. Фабричная механика включается только при достижении хотя бы одного гейта:

- независимо поставляемых компонентов ≥ 3;
- вертикальных профилей ≥ 2;
- появились внешние потребители компонентов вне нашей команды.

До гейтов допустим standalone-режим: сборка и деплой по упрощённому манифесту без каталога и release trains. Построение каталога и поездов до гейта нарушает LAW-13.

---

## 5. Владение данными

Компонент владеет своими данными и отвечает за их жизненный цикл. Используется logical ownership, а не обязательное физическое разделение с первого дня.

### 5.1. Level 0

Допускается одна PostgreSQL с логическими схемами компонентов. При необходимости применяются отдельные схемы, роли, RLS и ограничения доступа.

### 5.2. Level 1+

Компоненты могут иметь физически отдельные БД. Контракт владения остаётся тем же.

### 5.3. Главное правило

Orders owns Orders data. Learning owns Learning data. Analytics owns Analytics data. Другой компонент не получает права на прямой доступ.

### 5.4. Мультиарендность и области данных

Platform Instance по умолчанию multi-tenant.

Каждый набор данных относится ровно к одной области:

- `tenant-scoped` — данные конкретного tenant; по умолчанию все бизнес-данные;
- `platform-scoped` — данные конкретной Platform Instance;
- `system-scoped` — данные самой фабрики и системного уровня.

Область объявляется в Component Contract. Кросс-tenant обработка `tenant-scoped` данных запрещена. Кросс-tenant операции допустимы только над `platform-scoped` или `system-scoped` данными, если они явно авторизованы и отражены в аудите.

### LAW-16a — Data Scope Classification

Любой набор данных обязан иметь ровно одну область. Нельзя менять область данных неявно. `tenant-scoped` данные не могут использоваться как общая платформа без отдельного архитектурного решения и изменения контракта.

Tenant-контекст выводится из проверенной идентичности. Переданный вызывающей стороной `tenant_id` является только cross-check и отклоняется при несовпадении. Исключение — явно авторизованные platform/system-scoped операции.

---

## 6. Контракты

### 6.1. Component Contract

Каждая версия компонента публикует машиночитаемый дескриптор публичного контракта минимум с полями:

`api`, `events`, `data_export_cdc`, `ui`, `configuration_schema`, `data_ownership`, `authn`, `authz`, `compatibility_policy`, `dependencies`.

Необъявленная поверхность является внутренней и не защищается контрактом совместимости.

### 6.2. SSO и Identity

OIDC является стандартным механизмом идентификации человека и единого входа. Сервис, фоновая задача и агент используют собственную проверяемую service identity.

Аутентификация не заменяет авторизацию. Авторизация применяется на границе компонента-владельца данных.

### 6.3. Events

События используются для асинхронной интеграции. Рекомендуемый envelope — CloudEvents.

Доставка событий — at-least-once. Консьюмеры обязаны быть идемпотентными. CloudEvents для tenant-scoped бизнес-событий содержат tenant context.

Межкомпонентные бизнес-процессы проектируются как саги с явными компенсациями. Атомарность между компонентами не предполагается и не изображается в UI.

### 6.4. Saga Contract

Минимальный контракт саги: `saga_id`, `correlation_id`, `tenant_id` либо явная scope, `step_id`, `state`, `retry_policy`, `timeout`, `compensation`, `idempotency`, `terminal_state`.

Компенсация объявляется до выполнения шага и сама идемпотентна. Терминальные состояния: `completed`, `compensated`, `failed`. Таймаут не является молчаливым успехом.

### 6.5. Идемпотентность

Определяются три класса:

1. события — `event_id` и дедупликация;
2. API — `Idempotency-Key` для неидемпотентных по природе операций изменения состояния;
3. команды — `command_id`, включая шаги саги.

Повтор с тем же ключом не создаёт второй эффект. Replay является отдельной операцией и не смешивается с retry.

### 6.6. API

API описывается OpenAPI. Совместимость проверяется contract testing. Рекомендуемый формат: `/api/v1/...`, `/api/v2/...`.

### 6.7. UI Embedding

По умолчанию: ссылки, страницы, виджеты. Microfrontend не является обязательным способом интеграции. UI extension работает через версионированный protocol.

### 6.8. Design Tokens

Общие дизайн-токены обеспечивают визуальную интеграцию компонентов.

### 6.9. Data Export / CDC

Analytics не получает прямого доступа к OLTP БД бизнес-компонентов. Для массовой загрузки и изменений используются bulk export, CDC и официальные data contracts. Версия и compatibility policy Data Export/CDC объявляются в Component Contract.

---

## 7. API Versioning

Компонент имеет одну публичную SemVer-версию, а API может иметь несколько поддерживаемых major versions.

Правила: additive change → minor; breaking change → major; одновременно поддерживается не более двух API major versions без отдельного решения; deprecation объявляется заранее; используется Sunset header.

---

## 8. Event Versioning

Событие имеет версию схемы, например `OrderCreated@v1`. Добавление необязательного поля обычно совместимо; изменение семантики — breaking; consumer использует tolerant reader; producer может временно dual-publish; поддерживаются текущий и предыдущий major event contract, если иное не утверждено.

---

## 9. Schema Registry

Источник истины для схем — registry. На Level 0 допускается пакет в монорепозитории с JSON Schema / Zod, owner, status и compatibility policy. Статусы: `draft`, `active`, `deprecated`, `retired`.

---

## 10. Contract Testing

Контракты обязательны для публикации компонента.

API: consumer/provider contract testing. Events: schema compatibility, schema diff и replay потребителей. UI: props, postMessage protocol и smoke rendering в shell.

Компонент не может попасть в Certified Golden Bundle, если обязательные contract tests не пройдены.

---

## 11. Component Version и идентификаторы

Публичная версия компонента — SemVer `MAJOR.MINOR.PATCH`.

Идентификаторы и версии разделяются для `platform_id`, `component_id`, `component_version_id`, `manifest_id`, `bundle_id`, `tenant_id`, `event_id`, `saga_id`, `request_id`, `correlation_id`, `contract_id`.

Идентификатор иммутабелен, непрозрачен, уникален в своей области, не переиспользуется и не кодирует бизнес-смысл. Для поставляемых артефактов обязательны `id + version + digest`. Конкретный формат UUIDv7/ULID и т.п. остаётся временным решением.

---

## 12. Database Migrations

Migration принадлежит компоненту. Production migration — FORWARD ONLY.

```
EXPAND → MIGRATE → CONTRACT
```

Новый код должен быть совместим со схемой после Expand. Contract выполняется не раньше следующего релиза после migrate, если rollback window требует совместимости.

CI обязан проверять миграционный путь `N-3 → N` и сценарий пропуска двух релизов после появления CI.

---

## 13. Rollback

Rollback — прежде всего возврат программного артефакта через предыдущий Platform Manifest.

> Code can roll back. Database schema normally does not.

Новые данные не уничтожаются ради rollback.

---

## 14. Golden Bundle

Golden Bundle — сертифицированный, воспроизводимый набор совместимых компонентов и версий.

Lifecycle: `draft → candidate → certified → deprecated → revoked`.

`certified` означает прохождение утверждённого набора проверок совместимости и безопасности. Поставляемый bundle иммутабелен и идентифицируется `bundle_id + version + digest`; evidence привязывается к digest. `revoked` запрещает новые поставки, но не означает автоматического разрушения уже работающих инстансов.

Создание bundle включает выбор стабильных версий, compatibility matrix, contract tests, E2E, migration paths, smoke/load validation и подпись артефакта.

---

## 15. Compatibility Matrix

Factory ведёт матрицу совместимости. Полная комбинаторика не тестируется: используются isolated component tests, contract tests, Golden Bundle E2E, pairwise/nightly testing и canary validation на реальных типах Platform Instances.

---

## 16. Platform Manifest

Manifest — главный артефакт конкретной платформы. Он фиксирует Component versions, Golden Bundle, Configuration, Extensions, Branding и image digests.

Lifecycle: `draft → validated → approved → published → deployed → superseded → retired`.

Для standalone допускается упрощённый профиль с одной записанной аттестацией валидации. `published` иммутабелен и содержит `manifest_id + version + digest + predecessor`. `approved` содержит сведения об утверждении. Опубликованный manifest нельзя переписывать под тем же identity/version.

---

## 17. Composer

Composer разрешает зависимости, проверяет совместимость, выбирает версии, применяет configuration, проверяет extensions, формирует воспроизводимый Manifest и передаёт его на validation.

Composer не должен скрыто заменять версии. Любое изменение состава должно быть видно в diff Manifest.

---

## 18. Dependency Graph

Разрешено: `A → B API`; `A → B Events`; `A → B Export / CDC`.

Запрещено: `A → B Database`; `A → B internal code`; `A → B private schema`; `A → B internal queue`.

Каждая зависимость объявляется, версионируется, проверяется и трассируется до конкретного контракта.

---

## 19. Component Lifecycle

```
draft → development → candidate → certified → active → deprecated → retired
```

Версия и статус — разные понятия.

---

## 20. Configuration

Конфигурация декларативна и версионируется вместе с компонентом. Неизвестный configuration key — BUILD ERROR.

Секреты не входят в Platform Manifest. Окружения используют overlays.

---

## 21. Extensions

Extension работает только через публичный контракт компонента. Допустимы webhook, custom fields, UI widget, theme, plugin/custom component и официальные extension APIs.

---

## 22. Customer Fork Policy

Fork запрещён по умолчанию. Перед fork необходимо доказать, что задача не решается Configuration, Extension, Plugin, Custom Component или Feature в основном компоненте. Fork требует архитектурного решения.

---

## 23. Profiles

Фабрика может иметь вертикальные профили: commerce, education, logistics, media, analytics.

Профиль = Component set + Default configuration + UI templates + Golden Bundle.

Новый универсальный компонент обычно вводится в каталог, если он нужен минимум двум профилям или имеет отдельно обоснованную стратегическую ценность.

---

## 24. Аналитические компоненты

Analytics — полноценный потребитель контрактов. Он не получает прямого доступа к OLTP БД бизнес-компонентов.

Кросс-tenant аналитика допустима только над `platform-scoped` или `system-scoped` данными либо над заранее подготовленным агрегированным контрактом, который не раскрывает tenant-scoped данные и имеет соответствующую область. Любая операция должна быть авторизована и аудируема.

---

## 25. Масштабирование

Компоненты масштабируются независимо, если архитектура это позволяет. Особенно Computational Services, Streaming Pipelines, Analytics и High-load Business Systems.

---

## 26. Observability

Каждый production-компонент предоставляет health/readiness, metrics, structured logs, traces где применимо, correlation/request ID и version/build identifier.

Стандартный context, где применимо: `timestamp`, `environment`, `platform_id`, `component_id`, `component_version`, `tenant_id` для tenant-контекста, `request_id`, `trace_id`, `correlation_id`, `saga_id`, `actor/service_id`.

Контекст не должен позволять пересечь tenant boundary; чувствительные значения не передаются без необходимости.

---

## 27. Security

Секреты не хранятся в коде и не входят в Manifest. Доступ минимально необходимый. Внешние API проходят authentication/authorization. Зависимости регулярно проверяются. Security hotfix может выпускаться вне обычного release train.

Изменяющие состояние операции и tenant-scoped чтения подлежат аудиту. Аудит является отдельным неизменяемым журналом событий безопасности и изменения состояния.

---

## 28. Release Policy

Release Train — операционная политика, а не фундаментальная архитектурная зависимость. Фабрика допускает плановые Golden Bundle releases, security releases и critical hotfixes.

Отдельный `RELEASE_POLICY.md` создаётся, когда проект входит в фабричный режим и появляется реальная потребность в такой политике. До этого отсутствие такого документа не является нарушением архитектуры.

---

## 29. Upgrade

Рекомендуемый процесс:

```
New Manifest → Canary Instance → Smoke → Contract Tests →
E2E → Migration → Traffic Switch → Observation → Retire Old Version
```

Для production нельзя выполнять массовый upgrade без проверки.

---

## 30. Definition of Done для Component

Компонент считается готовым к публикации только если существуют: исходный код; API contract; data ownership definition; migrations; event contracts; contract tests; unit/integration tests; deployment artifact; configuration schema; documentation; health/readiness; version; owner; compatibility information.

Отсутствие обязательной части означает NOT PUBLISHABLE.

---

## 31. Definition of Done для Platform

Platform Instance считается воспроизводимой, если существует Platform Manifest; конкретные версии компонентов; Golden Bundle либо явный статус uncertified; image digests; configuration; extensions; branding; миграционный план; результаты обязательных проверок.

---

## 32. Правила для разработчиков

Разработчик обязан: определить бизнес-границу; владельца данных; публичные контракты; зависимости; миграционный путь; контрактные тесты; и только после этого выбирать внутреннюю техническую реализацию.

Нельзя начинать декомпозицию с вопроса «Какой микросервис здесь сделать?». Первый вопрос: «Какую самостоятельную бизнес-возможность мы реализуем и кто ею владеет?».

---

## 33. Правила для AI-инструментов

AI coding agent обязан считать этот документ источником архитектурных ограничений.

AI запрещено самостоятельно менять архитектурные границы, создавать прямой доступ к чужой БД, делать незаявленный breaking change, создавать Customer Fork, удалять migration ради исправления production schema, заменять pinned version на latest, добавлять микросервисы без обоснования или менять фундаментальные архитектурные правила без ADR.

AI обязан соблюдать component boundaries и data ownership, сохранять backward compatibility, писать/обновлять contract tests, учитывать migrations, обновлять документацию и явно сообщать о конфликте с законом.

---

## 34. Architecture Decision Record

Фундамент меняется только через ADR:

```
docs/adr/
├── ADR-0010-architecture-gap-review.md
└── ...
```

Каждый ADR содержит Context, Decision, Alternatives, Consequences, Migration и Status.

### 34.1. Уровни изменений

- **Level A — implementation.** Архитектуру не меняет. ADR не нужен.
- **Level B — local architecture.** Меняет внутреннюю реализацию компонента. ADR компонента нужен, если решение существенно.
- **Level C — platform architecture.** Меняет component boundaries, contracts, data ownership, versioning, deployment model или factory behavior. Требуется ADR.
- **Level D — constitutional change.** Меняет фундаментальные правила этого документа. Требуются ADR, анализ затронутых компонентов, migration plan, проверка Golden Bundles при их наличии и явная ратификация новой версии ARCHITECTURE.md.

### 34.2. Правило происхождения

Каждая RATIFIED версия ARCHITECTURE.md должна иметь реальный ADR, существующий в `docs/adr/`. Отсутствующий исторический ADR не создаётся задним числом.

---

## 35. Что запрещено менять без ратификации

Без отдельного архитектурного решения запрещено менять Component definition, Data ownership law, Contract model, Versioning law, Migration law, Manifest model, Golden Bundle model, Customer Fork policy, Architecture maturity levels и Tenant isolation law.

---

## 36. Принцип эволюции

Мы фиксируем стабильные законы, а не временные технические детали.

Стабильные законы: ownership; boundaries; contracts; compatibility; reproducibility; versioning; migration safety; independent lifecycle; tenant isolation; auditability.

Временные решения: конкретный message broker; конкретный CI; конкретный deployment engine; конкретный release cadence; конкретная observability stack.

---

## 37. Главная модель всей системы

```
             SOFTWARE PRODUCT FACTORY
                       |
                       v
              COMPONENT CATALOG
                       |
         +-------------+-------------+
         |             |             |
         v             v             v
     Components    Extensions    Schemas
         |
         v
    Compatibility
       Matrix
         |
         v
    GOLDEN BUNDLES
         |
         v
      COMPOSER
         |
         v
   PLATFORM MANIFEST
         |
         v
    PLATFORM INSTANCE
```

---

## 38. Финальные архитектурные законы

- **LAW-01 — Business Boundary.** Границы определяются бизнес-возможностями, а не техническими классами.
- **LAW-02 — Component Ownership.** Каждый Component имеет одного владельца и самостоятельный lifecycle.
- **LAW-03 — Data Ownership.** Компонент является владельцем своих данных.
- **LAW-04 — No Direct Database Sharing.** Чужие данные доступны только через опубликованные контракты.
- **LAW-05 — Contract First.** Внешнее взаимодействие определяется контрактом.
- **LAW-06 — Independent Evolution.** Компонент должен иметь возможность развиваться независимо в пределах контрактов.
- **LAW-07 — Version Everything That Matters.** Компоненты, контракты, схемы и поставляемые платформы должны быть идентифицируемы и версионируемы.
- **LAW-08 — Forward Migration.** Production schema развивается вперёд.
- **LAW-09 — Reproducible Platform.** Каждая Platform Instance должна быть воспроизводима из Manifest.
- **LAW-10 — Certified Composition.** Golden Bundle является сертифицированной комбинацией версий.
- **LAW-11 — Configuration Before Fork.** Кастомизация начинается с configuration и extensions.
- **LAW-12 — Microservice Is Not a Product Boundary.** Микросервис — внутренняя реализационная единица, если иное не обосновано.
- **LAW-13 — Complexity Must Be Earned.** Распределённость вводится только тогда, когда она решает реальную проблему.
- **LAW-14 — Architecture Changes Through ADR.** Фундаментальная архитектура изменяется только контролируемо.
- **LAW-15 — Reversibility.** Архитектурные решения должны по возможности сохранять возможность безопасной эволюции и консолидации.
- **LAW-16 — Tenant Isolation.** Изоляция арендаторов — часть закона владения данными: tenant-scoped данные чужого арендатора недоступны через прямой доступ или контракт без законного tenant-контекста.

---

## 39. Финальная формула

Мы не создаём новую платформу с нуля для каждого клиента. Мы создаём новую композицию из существующих и сертифицированных строительных блоков.

```
Configuration
    ↓
Extension
    ↓
New Component
    ↓
New Golden Bundle
    ↓
New Platform Manifest
```

Customer Fork — исключение, а не нормальный путь развития.

---

## 40. Статус и доказательная база

На 2026-09-08 архитектурный аудит выявил 34 замечания F-001…F-034, зарегистрированные в ADR-0010. ADR-0010 ратифицирован владельцем проекта и является фактическим основанием настоящей редакции 1.2.0.

Исторические утверждения о ранее выполненной сценарной валидации, которые ссылались на отсутствующие документы, удалены из нормативного закона. Это означает не «валидация не нужна», а то, что в текущем репозитории она не считается выполненной только по неподтверждённой ссылке.

Первичная implementation-валидация выполняется после Documentation Baseline и должна оставлять проверяемые артефакты. Любое выявленное нарушение фундаментального закона оформляется через ADR.

---

## 41. Changelog

| Версия | Статус | Изменения |
|---|---|---|
| 1.0.0 | draft | Исходная редакция |
| 1.1.0 | superseded | Редакция с дополнительными правилами, происхождение которой не подтверждено отдельным сохранившимся ADR; её неподтверждённые ссылки не являются источником истины |
| 1.2.0 | **RATIFIED** | ADR-0010: устранён provenance gap; очищена модель документации; формализованы Component Contract, Tenant/data scopes, LAW-16a, tenant-context integrity, Saga Contract, идемпотентность, identity/authz, unified identifiers, Manifest lifecycle, Golden Bundle lifecycle, observability context, data lifecycle/audit и conformance; удалены неподтверждённые заявления о выполненной сценарной валидации |

---

## 42. Conformance baseline

До появления первого компонента проект находится в **standalone / foundation mode**.

Машиночитаемое соответствие будет вводиться по мере появления реализации. Для каждого правила, которое можно проверить машинно, должна существовать automated check, contract test либо зарегистрированная manual review procedure.

Level C/D изменения требуют ADR. RATIFIED архитектура не изменяется прямым редактированием без соответствующего ADR и новой ратификации.
