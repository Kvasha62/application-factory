# ARCHITECTURE.md

# Фундаментальная архитектура «Фабрики сборных программных продуктов»

**Версия:** 1.1.0
**Статус:** RATIFIED — технический закон проекта
**Дата:** 2026-09-08
**Область действия:** вся дальнейшая разработка Platform Factory, Component Catalog, компонентов, Golden Bundles и Platform Instances.
**Изменения 1.0.0 → 1.1.0:** ADR-0009 (добавлены §0.2, §4.1, §5.4, LAW-16, правила саг и идемпотентности в §6.2, уточнены §16, §34, §40; добавлен §41 Changelog).

---

## 0. Назначение документа

Этот документ является техническим законом проекта.

Он определяет:

- что именно мы строим;
- основные архитектурные понятия;
- границы компонентов;
- владение данными;
- API, события и другие контракты;
- правила версионирования;
- правила миграций;
- правила сборки платформ;
- правила обновления и отката;
- правила кастомизации;
- правила развития от монолита к распределённой архитектуре;
- что разработчикам и AI-инструментам разрешено и запрещено делать.

### 0.1. Главный принцип

Мы строим не одну монолитную программу и не набор случайных микросервисов.

Мы строим фабрику повторно используемых программных компонентов, из которых можно воспроизводимо собирать различные самостоятельные программные продукты.

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
                       |
        +--------------+--------------+
        |              |              |
        v              v              v
     Customer A    Customer B    Customer C
```

### 0.2. Соотношение документов проекта *(введено ADR-0009)*

Закон — верхний слой; он не заменяет, а связывает остальные документы:

| Слой | Документ | Роль |
|---|---|---|
| Закон | **ARCHITECTURE.md** (этот) | обязательные правила, законы LAW-01…16 |
| Толковый словарь модели | `docs/07-core-model.md` | определения и механизмы, утверждённые валидацией |
| Сценарная валидация | `docs/08-scenario-validation.md` | доказательная база (дефекты F1–F6) |
| Продуктовая архитектура | `docs/01-architecture.md` | конкретика уровня 0 (стек, модули, данные) |
| Стек и инструменты | `docs/04-tech-stack.md` | «временные решения» по смыслу §36 |

При конфликте приоритет у этого документа; продукты (docs/01, docs/04) приводятся к нему через ADR.

---

## 1. Архитектурная конституция

Следующие правила являются **не** рекомендациями, а обязательными архитектурными ограничениями.

### 1.1. Нельзя нарушать границы владения

Компонент не имеет права напрямую читать или изменять внутреннюю БД другого компонента.

Разрешено взаимодействие только через опубликованные контракты:

- API;
- события;
- Data Export / CDC;
- официальные интеграционные механизмы.

Запрещено:

- SQL в чужую БД;
- импорт внутренних Python/C#/TypeScript-модулей другого компонента;
- обращение к внутренним таблицам;
- использование внутренних очередей;
- зависимость от неописанных внутренних структур.

### 1.2. Нельзя создавать Customer Fork без крайней необходимости

Порядок кастомизации:

```
Configuration
     ↓
Extension
     ↓
Plugin / Custom Component
     ↓
изменение самого компонента
     ↓
Fork — только как исключение
```

Fork считается архитектурным долгом и требует отдельного решения.

### 1.3. Нельзя использовать latest как архитектурную зависимость

Платформа всегда собирается из конкретных версий компонентов.

Продакшен-образы фиксируются по digest.

### 1.4. Нельзя делать breaking change незаметно

Breaking change требует:

- новой major-версии компонента либо другой явно утверждённой стратегии совместимости;
- миграционного плана;
- проверки контрактов;
- плана перехода потребителей.

### 1.5. Нельзя откатывать production database разрушительно

Production migrations — forward-only.

Rollback выполняется откатом программного артефакта/манифеста, а не удалением уже применённой схемы и данных.

### 1.6. Нельзя вводить микросервис только ради «красивой архитектуры»

Microservice — внутренняя единица деплоя.

Границы продукта определяются бизнес-возможностями, а не количеством процессов.

---

## 2. Иерархия понятий

В документации проекта термины используются строго.

```
PLATFORM FACTORY
│
├── COMPONENT CATALOG
│   └── COMPONENT
│
├── GOLDEN BUNDLES
│
├── COMPOSER
│
├── RELEASE POLICY
│
└── PLATFORM INSTANCE
    ├── Manifest
    ├── Configuration
    ├── Extensions
    └── Branding
```

### 2.1. Platform Factory

Фабрика — наша система разработки, регистрации, проверки, сборки, версионирования и поставки компонентов.

Фабрика не является конкретным продуктом заказчика.

### 2.2. Platform Instance

Platform Instance — конкретный развёрнутый продукт заказчика.

Он определяется:

```
Platform Instance =
    Platform Manifest
  + Configuration
  + Extensions
  + Branding
  + Environment-specific settings
```

### 2.3. Component

Component — основная единица каталога и поставки.

Каждый компонент имеет паспорт:

```
Code
API
Data Ownership
Migrations
Events
Contracts
Tests
Documentation
```

У компонента:

- один владелец;
- один внешний контрактный контур;
- один жизненный цикл;
- одна публичная SemVer-версия.

Внутреннее устройство компонента потребителям не гарантируется.

### 2.4. Microservice

Microservice — внутренняя единица реализации и деплоя компонента.

```
Component
├── API
├── Domain
├── Data
└── internal services
    ├── service A
    ├── service B
    └── worker C
```

Микросервис не становится самостоятельным компонентом только потому, что является отдельным процессом.

Если внутренняя часть требует:

- собственного публичного контракта;
- самостоятельного жизненного цикла;
- самостоятельного релизного цикла;
- независимого использования;

она может быть выделена в отдельный Component.

---

## 3. Классы компонентов

Компоненты делятся на четыре класса.

| Класс | UI | Собственные данные | Примеры |
|---|---|---|---|
| **A. Business System / SCS** | Да | Да | Learning, Commerce, Booking |
| **B. Platform Service** | Нет / служебный UI | Да | Identity, Payments, Media, Notifications |
| **C. Computational Service** | Нет | Опционально | Pricing Engine, Route Optimizer |
| **D. Streaming Pipeline** | Нет | Собственный state | Telemetry Ingest, Analytics Pipeline |

### 3.1. Business System

Самостоятельная бизнес-система. Способна работать независимо от конкретной платформы.

### 3.2. Platform Service

Общий сервис, используемый несколькими бизнес-компонентами: Identity, Payments, Media, Notifications.

### 3.3. Computational Service

Вычислительная способность, обычно stateless. Может горизонтально масштабироваться независимо.

### 3.4. Streaming Pipeline

Высокочастотный потоковый компонент. Для него допускается отдельный state и потоковая инфраструктура.

---

## 4. Уровни архитектурной зрелости

Мы не обязаны начинать с микросервисов.

### Level 0 — Modular Monolith

```
ONE APPLICATION
│
├── Identity module
├── Orders module
├── Learning module
├── Payments module
└── Analytics module
```

Физически может использоваться одна PostgreSQL. Границы владения данными при этом сохраняются логически.

### Level 1 — Independent Applications

Компоненты выделяются в отдельные приложения, когда есть реальная причина:

- две и более команды;
- независимый release cycle;
- другой профиль нагрузки;
- необходимость независимого масштабирования;
- необходимость самостоятельной поставки.

### Level 2 — Component Factory

Появляются: Component Catalog; Component Registry; Golden Bundles; Composer; Platform Manifest; независимая поставка компонентов.

### Level 3 — Internal Microservices

Крупный компонент может быть дополнительно декомпозирован на микросервисы. Это оптимизация реализации, а не обязательная архитектурная цель.

**Важное правило.** Обратная консолидация разрешена: если распределённый компонент больше не получает преимуществ от самостоятельного существования, его можно объединить обратно.

### 4.1. Гейты фабричного режима *(введено ADR-0009)*

Манифест платформы обязателен всегда (LAW-09) — он нужен даже в монолите.

Фабричная механика (Component Catalog, сертификация Golden Bundles, Composer, Release Train) включается **только** при достижении хотя бы одного гейта:

- независимо поставляемых компонентов ≥ 3;
- вертикальных профилей ≥ 2;
- появились внешние потребители компонентов вне нашей команды.

До гейтов допустим **standalone-режим**: сборка и деплой по упрощённому манифесту (compose / values), без каталога и поездов. Построение каталога и поездов до гейта — нарушение LAW-13.

---

## 5. Владение данными

Главный закон:

> Компонент владеет своими данными и отвечает за их жизненный цикл.

Мы используем модель logical ownership, а не обязательное физическое разделение с первого дня.

### 5.1. Level 0

Допускается:

```
PostgreSQL
├── identity schema
├── orders schema
├── learning schema
└── analytics schema
```

При необходимости применяются: отдельные схемы; роли; RLS; ограничения доступа.

### 5.2. Level 1+

Компоненты могут иметь физически отдельные БД.

### 5.3. Главное правило

Неважно, физически одна БД или десять:

- Orders owns Orders data.
- Learning owns Learning data.
- Analytics owns Analytics data.

Другой компонент не получает права на прямой доступ.

### 5.4. Мультиарендность *(введено ADR-0009)*

Platform Instance по умолчанию **multi-tenant**: арендатор (tenant) — конечный клиент платформы (магазин, школа). Правила:

- контекст арендатора выдаётся Identity и обязателен во всех компонентах;
- Level 0: изоляция логическая — `tenant_id` + RLS в схеме каждого компонента;
- Level 1+: компонент может выбрать физическое разделение, контракт изоляции сохраняется;
- каждое событие, экспорт/CDC-запись и трейс содержат `tenant_id`;
- суб-арендатор (например, продавец маркетплейса) — внутреннее понятие компонента, а не платформы (docs/08, стойка F5).

---

## 6. Контракты

Архитектура использует шесть основных контрактов.

### 6.1. SSO

OIDC является стандартным механизмом идентификации и единого входа.

### 6.2. Events

События используются для асинхронной интеграции.

Рекомендуемый envelope: CloudEvents.

На начальных уровнях допускаются task channel и event channel. Для потоковой аналитики не следует превращать обычную очередь задач в универсальный telemetry bus; потоковый канал (стрим-брокер, например NATS JetStream) вводится на Level 2. *(уточнено ADR-0009)*

Дополнительно *(введено ADR-0009)*:

- межкомпонентные бизнес-процессы проектируются как **саги с явными компенсациями**; атомарность между компонентами не предполагается и не изображается в UI;
- доставка событий — **at-least-once**: консьюмеры обязаны быть **идемпотентны** (дедупликация по идентификатору события);
- CloudEvents envelope всегда содержит `tenant_id` (см. §5.4).

### 6.3. API

API описывается OpenAPI. Совместимость проверяется contract testing.

### 6.4. UI Embedding

По умолчанию: ссылки; страницы; виджеты. Microfrontend не является обязательным способом интеграции.

### 6.5. Design Tokens

Общие дизайн-токены обеспечивают визуальную интеграцию компонентов.

### 6.6. Data Export / CDC

Аналитические системы не читают чужие БД. Для массовой загрузки и отслеживания изменений используются: bulk export; CDC; официальные data contracts.

---

## 7. API Versioning

Компонент имеет одну публичную SemVer-версию.

API может иметь несколько поддерживаемых major API versions.

Рекомендуемый формат:

```
/api/v1/...
/api/v2/...
```

Правила:

- additive change → minor;
- breaking change → major;
- одновременно поддерживается не более двух API major versions без отдельного решения;
- deprecation объявляется заранее;
- используется Sunset header;
- потребители должны переходить на новую версию до окончания окна поддержки.

Нельзя: «v1 сегодня работает, v1 завтра молча поменял смысл».

---

## 8. Event Versioning

Событие имеет версию схемы.

```
OrderCreated@v1
OrderCreated@v2
```

Правила:

- добавление необязательного поля обычно совместимо;
- изменение семантики — breaking;
- consumer должен использовать tolerant reader;
- producer может временно dual-publish старую и новую версии;
- поддерживается текущий и предыдущий major event contract, если иное не утверждено явно.

---

## 9. Schema Registry

Источник истины для схем — registry.

### Level 0

Допускается пакет в монорепозитории: `@platform/schemas`.

Содержит: JSON Schema / Zod; owner; status; compatibility policy.

Статусы: `draft`, `active`, `deprecated`, `retired`.

### Более высокие уровни

Registry может быть выделен в отдельный сервис. При этом источник истины остаётся один.

---

## 10. Contract Testing

Контракты обязательны для публикации компонента.

**API.** Используется consumer/provider contract testing, например Pact.

**Events.** Проверяется: schema compatibility; schema diff; replay потребителей.

**UI.** Проверяются: props; postMessage protocol; smoke rendering в shell.

**Закон.** Компонент не может попасть в Certified Golden Bundle, если обязательные contract tests не пройдены.

---

## 11. Component Version

Публичная версия компонента — SemVer: `MAJOR.MINOR.PATCH`.

Например: Orders 4.2.1.

Версия компонента является идентификатором его поставляемого релиза. Внутри неё могут существовать независимые технические идентификаторы:

```
Component:      4.2.1
API:            v2
DB schema:      37
Events:         OrderCreated@v2, OrderPaid@v1
```

Эти номера не обязаны совпадать.

---

## 12. Database Migrations

Migration принадлежит компоненту.

Правило: **FORWARD ONLY**.

Production schema не откатывается назад разрушительной migration.

Используется:

```
EXPAND
   ↓
MIGRATE
   ↓
CONTRACT
```

### 12.1. Expand

Добавляются совместимые структуры: nullable columns; новые таблицы; новые индексы; новые поля. Старый код продолжает работать.

### 12.2. Migrate

Данные переводятся в новую форму.

### 12.3. Contract

Удаляются старые структуры. Contract выполняется не раньше следующего релиза после migrate, если rollback window требует сохранения совместимости.

### 12.4. CI

CI обязан проверять миграционный путь `N-3 → N` и сценарий пропуска двух релизов.

---

## 13. Rollback

Rollback — это прежде всего возврат программного артефакта: deploy previous Platform Manifest.

Ключевой закон:

> Code can roll back. Database schema normally does not.

Поэтому новый код обязан быть совместим со схемой после Expand.

Пример:

```
v4.1
  ↓
Expand
  ↓
v4.2
```

Должен существовать рабочий вариант «v4.1 + expanded schema» в пределах rollback window.

Данные, созданные новой версией, не должны уничтожаться ради rollback.

---

## 14. Golden Bundle

Golden Bundle — сертифицированный набор конкретных версий компонентов.

Он не равен `latest + latest + latest`. Он равен:

```
Identity 3.2.1
Orders   4.2.0
Payments 2.8.1
Analytics 5.1.0
```

Создание Golden Bundle:

- выбираются стабильные версии;
- проверяется compatibility matrix;
- выполняются contract tests;
- выполняется E2E;
- проверяются migration paths;
- выполняется smoke/load validation;
- артефакт подписывается;
- bundle публикуется в registry.

Golden Bundle является сертифицированной комбинацией, а не обязательным ограничением всех возможных технически совместимых комбинаций.

---

## 15. Compatibility Matrix

Factory ведёт матрицу совместимости.

| | Orders 4.1 | Orders 4.2 |
|---|---|---|
| Payments 2.7 | ✓ | ✓ |
| Payments 2.8 | ✓ | ✓ |
| Analytics 5.0 | ✓ | ✓ |
| Analytics 6.0 | — | ✓ |

Полная комбинаторика не тестируется. Используются: isolated component tests; contract tests; Golden Bundle E2E; pairwise/nightly testing; canary validation на реальных типах Platform Instances.

---

## 16. Platform Manifest

Manifest — главный артефакт конкретной платформы.

Он фиксирует: Component versions; Golden Bundle; Configuration; Extensions; Branding; Image digests.

Пример *(дополнен config и digests — ADR-0009)*:

```yaml
platform:
  name: customer-platform
  bundle: commerce-2026.09

  components:
    identity: 3.2.1
    orders: 4.2.0
    payments: 2.8.1
    analytics: 5.1.0

  images:
    orders: orders@sha256:0f1e…        # фиксация по digest
    payments: payments@sha256:9a8b…

  config:
    orders:
      locales: [fi, sv, en]
      currencies: [EUR]
    analytics:
      retention-days: 180

  extensions:
    - customer-notifications

  branding:
    theme: customer-theme
```

### 16.1. Иммутабельность

Опубликованный Manifest не изменяется. Изменение создаёт новую версию: Manifest 1.8 → 1.9 → 1.10.

Образы фиксируются digest (`image@sha256:...`), а не только mutable tags.

---

## 17. Composer

Composer создаёт Platform Manifest.

Он обязан:

- разрешить зависимости;
- проверить совместимость;
- выбрать версии;
- применить configuration;
- проверить extensions;
- сформировать воспроизводимый manifest;
- передать его на validation.

Composer не должен скрыто заменять версии. Любое изменение состава должно быть видимо в diff Manifest.

---

## 18. Dependency Graph

Компоненты взаимодействуют через контракты.

Разрешено: `A → B API`; `A → B Events`; `A → B Export / CDC`.

Запрещено: `A → B Database`; `A → B internal code`; `A → B private schema`; `A → B internal queue`.

Фабрика должна поддерживать граф зависимостей. Каждая зависимость должна быть:

- объявлена;
- версионирована;
- проверяема;
- трассируема до конкретного контракта.

Нельзя допускать скрытых зависимостей.

---

## 19. Component Lifecycle

Компонент имеет жизненный цикл:

```
draft → development → candidate → certified → active → deprecated → retired
```

Версия и статус — разные понятия:

- Orders 4.2.1 — active
- Orders 5.0.0 — candidate
- Orders 3.9.4 — deprecated

---

## 20. Configuration

Конфигурация декларативна.

Схема configuration версионируется вместе с компонентом.

Неизвестный configuration key: **BUILD ERROR**, а не молчаливое игнорирование.

Секреты не входят в Platform Manifest. Используются: Vault; secret manager; ссылки на секреты; environment-specific secret bindings.

Окружения используют overlays:

```
base + development overlay
base + staging overlay
base + production overlay
```

---

## 21. Extensions

Extension работает только через публичный контракт компонента.

Допустимые механизмы: webhook; custom fields; UI widget; theme; plugin/custom component; официальные extension APIs.

Extension не получает доступ к внутреннему коду. UI extension должен работать через версионированный protocol.

---

## 22. Customer Fork Policy

Fork запрещён по умолчанию.

Перед fork необходимо доказать, что задача не решается:

1. Configuration
2. Extension
3. Plugin
4. Custom Component
5. Feature в основном компоненте

Fork требует архитектурного решения.

Причина:

```
Fork
  ↓
отдельный lifecycle
  ↓
отдельные миграции
  ↓
отдельные compatibility tests
  ↓
потеря стандартного upgrade path
```

---

## 23. Profiles

Фабрика может иметь вертикальные профили: commerce; education; logistics; media; analytics.

Профиль — это: Component set + Default configuration + UI templates + Golden Bundle.

**Правило двух использований:** новый универсальный компонент обычно вводится в каталог, если он нужен минимум двум профилям или имеет отдельно обоснованную стратегическую ценность.

Это не запрет, а защита каталога от преждевременного усложнения.

---

## 24. Аналитические компоненты

Analytics — полноценные потребители контрактов. Они не получают прямого доступа к OLTP БД бизнес-компонентов.

```
Orders
   |
   +---- events ----+
   |                |
   +---- CDC -------+----> Order Analytics
   |                |
   +---- export ----+
```

Поэтому возможны самостоятельные продукты: Order Analytics; Course Completion Analytics; Transport Analytics; Blog Attendance Analytics — при сохранении границ владения данными.

---

## 25. Масштабирование

Компоненты масштабируются независимо, если их архитектура это позволяет.

Особенно: Computational Services; Streaming Pipelines; Analytics; High-load Business Systems.

Масштабирование не должно требовать масштабирования всей платформы без необходимости.

---

## 26. Observability

Каждый production-компонент обязан предоставлять стандартные эксплуатационные сигналы:

- health; readiness;
- metrics;
- structured logs;
- traces, где применимо;
- correlation/request ID;
- version/build identifier.

Минимум для диагностики: Platform; Component; Version; Request/Trace ID; Environment; Timestamp.

*(дополнение ADR-0009: в multi-tenant инстансах в этот минимум входит также `tenant_id`.)*

---

## 27. Security

Безопасность является частью контракта компонента.

Обязательные требования:

- секреты не хранятся в коде;
- секреты не входят в Manifest;
- доступ минимально необходимый;
- сервис не получает чужие DB credentials без архитектурного основания;
- внешние API проходят authentication/authorization;
- зависимости регулярно проверяются;
- security hotfix может выпускаться вне обычного release train.

---

## 28. Release Policy

Release Train — операционная политика, а не фундаментальная архитектурная зависимость.

Фабрика допускает: плановые Golden Bundle releases; security releases; critical hotfixes.

Конкретная периодичность определяется отдельным документом: `RELEASE_POLICY.md`.

Архитектура не зависит от фиксированного количества недель между релизами.

---

## 29. Upgrade

Рекомендуемый процесс:

```
New Manifest
     ↓
Canary Instance
     ↓
Smoke
     ↓
Contract Tests
     ↓
E2E
     ↓
Migration
     ↓
Traffic Switch
     ↓
Observation
     ↓
Retire Old Version
```

Для production нельзя выполнять массовый upgrade без проверки.

---

## 30. Definition of Done для Component

Компонент считается готовым к публикации только если существуют:

- исходный код;
- API contract;
- data ownership definition;
- migrations;
- event contracts;
- contract tests;
- unit/integration tests;
- deployment artifact;
- configuration schema;
- documentation;
- health/readiness;
- version;
- owner;
- compatibility information.

Отсутствие обязательной части означает: **NOT PUBLISHABLE**.

---

## 31. Definition of Done для Platform

Platform Instance считается воспроизводимой, если существует:

- Platform Manifest;
- конкретные версии компонентов;
- Golden Bundle либо явный статус uncertified;
- image digests;
- configuration;
- extensions;
- branding;
- миграционный план;
- результаты обязательных проверок.

---

## 32. Правила для разработчиков

Разработчик обязан:

1. сначала определить бизнес-границу;
2. определить владельца данных;
3. определить публичные контракты;
4. определить зависимости;
5. определить миграционный путь;
6. написать тесты контрактов;
7. только после этого выбирать внутреннюю техническую реализацию.

Нельзя начинать декомпозицию с вопроса «Какой микросервис здесь сделать?».

Первый вопрос: «Какую самостоятельную бизнес-возможность мы реализуем и кто ею владеет?».

---

## 33. Правила для AI-инструментов

Любой AI coding agent обязан считать этот документ источником архитектурных ограничений.

AI **запрещено** самостоятельно:

- менять архитектурные границы;
- создавать прямой доступ к чужой БД;
- менять публичный контракт breaking-образом;
- создавать Customer Fork;
- удалять migration ради исправления production schema;
- заменять pinned version на latest;
- добавлять микросервисы без обоснования;
- менять фундаментальные архитектурные правила без Architecture Decision Record.

AI **обязан**:

- соблюдать Component boundaries;
- соблюдать data ownership;
- сохранять backward compatibility;
- писать/обновлять contract tests;
- учитывать migrations;
- обновлять документацию;
- явно сообщать о конфликте с архитектурным законом.

---

## 34. Architecture Decision Record

ARCHITECTURE.md не должен переписываться из-за каждого изменения.

Фундамент меняется только через ADR:

```
docs/adr/
├── ADR-0009-architecture-ratification.md
├── ADR-0010-...
└── ...
```

Каждый ADR содержит: Context; Decision; Alternatives; Consequences; Migration; Status.

*(дополнение ADR-0009: исторические ADR-0001…0008 ведутся в прежнем формате в `docs/adr.md`; новые ADR — в каталоге `docs/adr/`. Миграция старых записей в каталог — при первом удобном касании, не блокирует работу.)*

### 34.1. Уровни изменений

- **Level A — implementation.** Архитектуру не меняет. ADR не нужен.
- **Level B — local architecture.** Меняет внутреннюю реализацию компонента, не меняя внешних правил. Нужен ADR компонента, если решение существенно.
- **Level C — platform architecture.** Меняет: component boundaries; contracts; data ownership; versioning; deployment model; factory behavior. Требуется ADR.
- **Level D — constitutional change.** Меняет фундаментальные правила этого документа. Требует: нового ADR; анализа затронутых компонентов; migration plan; проверки Golden Bundles; явной ратификации новой версии ARCHITECTURE.md.

---

## 35. Что запрещено менять без ратификации

Без отдельного архитектурного решения запрещено менять:

- Component definition
- Data ownership law
- Contract model
- Versioning law
- Migration law
- Manifest model
- Golden Bundle model
- Customer Fork policy
- Architecture maturity levels
- *Tenant isolation law* *(добавлено ADR-0009)*

---

## 36. Принцип эволюции

Мы не пытаемся заранее предсказать всю будущую систему. Мы фиксируем стабильные законы, а не временные технические детали.

**Стабильные законы:** ownership; boundaries; contracts; compatibility; reproducibility; versioning; migration safety; independent lifecycle; *tenant isolation* *(ADR-0009)*.

**Временные решения:** конкретный message broker; конкретный CI; конкретный deployment engine; конкретный release cadence; конкретная observability stack.

Временные решения могут заменяться без переписывания архитектурной конституции, если сохраняются фундаментальные контракты.

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
         +----------------------+
         |                      |
         v                      v
   Configuration          Extensions
         |                      |
         +----------+-----------+
                    |
                    v
            PLATFORM INSTANCE
                    |
    +---------------+---------------+
    |               |               |
    v               v               v
 Business       Platform        Analytics
 Systems        Services        Pipelines
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
- **LAW-16 — Tenant Isolation** *(введён ADR-0009)*. Изоляция арендаторов — часть закона владения данными: нет доступа к данным чужого арендатора ни напрямую, ни через контракты без tenant-контекста.

---

## 39. Финальная формула

Наша система должна позволять сделать следующее:

```
            ONE FACTORY
                 |
      +----------+----------+
      |          |          |
   Learning   Commerce   Logistics
      |          |          |
      +----------+----------+
                 |
           Components
                 |
           Golden Bundles
                 |
          Platform Manifest
                 |
      +----------+----------+
      |          |          |
  Customer A Customer B Customer C
```

При этом:

- Мы не создаём новую платформу с нуля для каждого клиента.
- Мы создаём **новую КОМПОЗИЦИЮ** из уже существующих и сертифицированных строительных блоков.

И если клиенту нужна новая возможность, мы по возможности:

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

а не:

```
копия всей платформы
    ↓
ручные изменения
    ↓
Customer Fork
    ↓
невозможность нормально обновлять
```

---

## 40. Статус ратификации и сценарная валидация

Сценарная валидация **выполнена 2026-09-08** (docs/08-scenario-validation.md): Education Platform ✓; E-commerce ✓ (стойка F5); Transport ◐→✓ (дефекты F1, F3); Analytics ◐→✓ (дефект F2); Marketplace ✓. Найденные дефекты F1–F6 устранены в базовой модели (docs/07): классы компонентов, контракт Data Export/CDC, два канала событий, саги checkout, суб-арендатор, гейты сложности.

Ревью настоящей версии 1.0.0 выявило дополнительные пробелы F7–F10, устранённые ADR-0009 в редакции 1.1.0 (см. §41). Если будущая сценарная проверка обнаружит нарушение фундаментального закона — создаётся ADR.

Нельзя просто молча переписывать ARCHITECTURE.md под текущую реализацию. Если архитектура действительно должна измениться, изменение должно быть:

```
обнаружено → обосновано → описано ADR → проверено → ратифицировано
→ отражено новой версией ARCHITECTURE.md
```

Это и есть механизм, который защищает проект от постепенного расползания архитектуры.

---

## 41. Changelog *(введён ADR-0009)*

| Версия | Статус | Изменения |
|---|---|---|
| 1.0.0 | draft | Исходная редакция (предложение) |
| 1.1.0 | **RATIFIED** | ADR-0009: §0.2 (соотношение документов); §4.1 (гейты фабричного режима); §5.4 (мультиарендность); §6.2 (саги, идемпотентность, tenant_id в событиях); §16 (config + digests в манифесте); §26 (tenant_id в observability); §34 (миграция ADR); §35–36 (tenant isolation); LAW-16; §40 (актуализация статуса валидации) |
