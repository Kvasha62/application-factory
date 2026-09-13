# SCS-002 Commerce — Product catalog foundation (Stage 2 skeleton)

**Класс:** A — Business System / SCS
**Уровень:** Level 0 — Modular Monolith
**Версия компонента:** 0.1.0
**Владелец данных:** `commerce` (логическая схема `commerce`)
**Задача:** SCS-002 Commerce MVP Stage 2 — Issue #55
**Архитектурная основа:** `docs/ARCHITECTURE.md` v1.2.0 §5.3, §5.4, §6.1, §6.2,
§6.5, §26, §27, LAW-03, LAW-04, LAW-16, LAW-16a; ADR-0013

SCS-002 — второй независимый бизнес-компонент проекта после Learning,
владелец коммерческих данных. Этап 2 устанавливает компонент: identity,
публичный контракт, доменный/хранилищный фундамент всего MVP-контура
ADR-0013 и цепочку контроля. Живая поверхность — ровно две операции:

```text
Subject → Create one owned Product (этап 2)
Subject → Read one owned Product (этап 2)
```

Это не магазин «под ключ», не payments-платформа, не CRM, не склад, не
доставка, не аналитика и не generic CRUD: компонент владеет коммерческими
фактами (что продаётся, по какой цене, кто что выбрал, кто что купил и
оплачено ли это) и ровно одной границей контроля.

## Граница Product → Offer → Price

Каталог — это три разных понятия, а не три поля одной записи:

- **Product** — что может быть предложено к продаже в тенанте.
  Domain-agnostic: никаких Learning-сущностей, никаких внешних ключей в
  чужую схему и никакой цены. Цена продаваемого предложения живёт на
  записи Price, а не здесь;
- **Offer** — продаваемое предложение ровно одного Product того же тенанта;
- **Price** — отдельная цена предложения: сумма в минорных единицах и код
  валюты. Записи Price — append-only история: новая цена регистрируется
  новой записью и никогда не переписывает прошлую.

Свернуть границу в `Product.price` запрещено (инвариант C-009). Price
books, tiers, pricing engines и promotions отсутствуют — сложность должна
быть заработана (LAW-13).

## Order: факты покупки и payment state

```text
Order
├── purchase facts — immutable after creation
└── payment state  — mutable lifecycle/result
```

Факты покупки (buyer, тенант, позиции, зафиксированные цены) неизменяемы
после создания: записи заморожены, операций изменения у хранилища нет.
Каждая позиция (OrderLine) несёт **снепшот цены на момент покупки** —
скопированные amount/currency плюс ссылки Product/Offer; исторический
Order никогда не пересчитывается из текущего каталога.

Payment state (`PENDING_PAYMENT → PAID`) — изменяемая часть Order,
коммерческое состояние/результат оплаты, принадлежащее Commerce. Payment
Service, провайдер и gateway-имитация отсутствуют и не планируются без
отдельного решения.

## Cart и Checkout

Cart — изменяемое предзаказное состояние покупателя (непрозрачный
`buyer_identity_id`), не коммерческий факт: может меняться, пустеть и
бросаться без последствий. Checkout — **бизнес-команда, а не сущность**:
персистентного Checkout не существует. Команды корзины, checkout, заказа
и payment state появятся в следующих срезах; их модели, инварианты
хранилища, форма снепшота и словарь payment state уже зафиксированы.

## Покупатель

Покупатель — непрозрачная ссылка `buyer_identity_id` в существующую
Identity. Customer, User, Profile и второй identity-механизм отсутствуют;
внутренние данные Identity не копируются. Аутентификация — существующий
механизм платформы: Commerce сам не проверяет никаких credentials.

## Цепочка, которая доказывается

```text
Request (subject credential, record id, operation, [claimed tenant])
      ↓
ownership_boundary       запись существует здесь и её единственный
                         владелец — этот компонент        иначе DENY
      ↓
authorization_decision   IS-003 решает через порт; исход по умолчанию —
                         DENY; зависимость, которая не ответила или
                         ответила вне контракта, закрывает доступ
      ↓
owned_data_operation     только теперь читаются или пишутся
                         собственные данные
```

ALLOW от IS-003 — не доступ к данным: операция выполняется только здесь,
в конце цепочки. Эффективный тенант выводится из verified identity через
IS-001; `X-Tenant-Id` — только cross-check (LAW-16a). Команда создания
передаётся в IS-005 ровно один раз: повтор с тем же `Idempotency-Key`
возвращает записанный результат без второго эффекта, повтор с другим
binding отказывается. Обслужённые доступы и каждый отказ записываются в
собственный append-only журнал компонента с `request_id` и
`correlation_id`.

## Модель данных

### Product (этап 2)

| Поле | Комментарий |
|---|---|
| `product_id` | непрозрачный идентификатор |
| `tenant_id` | факт хранилища, не claim вызывающей стороны |
| `owner_component` | всегда `commerce` |
| `name` | непустое, не длиннее 512 символов |
| `description` | не длиннее 4096 символов |
| `status` | ровно `ACTIVE` |
| `created_by` | непрозрачная ссылка на verified identity |
| `created_at` / `updated_at` | timestamps |

Публичное представление — те же поля без `tenant_id` и `owner_component`.

### Фундамент следующих срезов (домен + хранилище, без команд)

| Запись | Инвариант |
|---|---|
| `Offer` | ровно один Product того же тенанта; сирота и чужой тенант невозможны |
| `Price` | ровно один Offer того же тенанта; amount — неотрицательный int в минорных единицах; append-only |
| `Cart` | один тенант, один `buyer_identity_id`; изменяемое состояние, не факт |
| `CartLine` | не более одной строки на (Cart, Offer); количество — положительное |
| `Order` | факты покупки заморожены; меняется только `payment_state` |
| `OrderLine` | снепшот amount/currency + ссылки Product/Offer; обновлений нет |

## Публичный контракт

- HTTP: `POST /products` (команда `commerce.products.create`,
  `Idempotency-Key` обязателен) и `GET /products/{product_id}` (операция
  `commerce.products.read`) — см. `contract/openapi.yaml`
  (servers: `/api/v1/commerce`, относительные пути) и
  `contract/component_contract.json`;
- уровень 0 (in-process): `commerce_service.reader.CommerceClient` — те же
  две операции, значения туда и обратно.

`Authorization` несёт credential субъекта: компонент сам не проверяет
никаких credentials. `X-Tenant-Id` — только cross-check (LAW-16a), он никогда
не выбирает эффективный тенант команды.

## Отказы

Отказы используют утверждённый конверт SCS-002:

```json
{
  "error": {
    "code": "AUTHORIZATION_DENIED",
    "message": "Operation is not permitted.",
    "details": {"reason": "permission_not_granted"}
  },
  "request_id": "opaque-id",
  "correlation_id": "opaque-id"
}
```

| Статус | Код | Причины (`error.details.reason`) |
|---|---|---|
| `400` | `IDEMPOTENCY_KEY_REQUIRED` | `idempotency_key_required` — команда без ключа |
| `401` | `AUTHENTICATION_REQUIRED` | `missing_identity`, `invalid_identity`, `unknown_identity` |
| `403` | `AUTHORIZATION_DENIED` | опубликованные `DENY`-причины IS-003 без изменений; `owner_mismatch` |
| `404` | `NOT_FOUND` | `product_unknown` — решение не запрашивается |
| `409` | `IDEMPOTENCY_CONFLICT` | `idempotency_conflict` — ключ уже использован с другим binding |
| `422` | `VALIDATION_ERROR` | `validation_error` — payload не проходит правила команды (пустое/длинное имя) |
| `503` | `DEPENDENCY_UNAVAILABLE` | `authorization_unavailable` — зависимость не ответила или вне контракта |

Статус выбирает код; стабильная внутренняя причина сохраняется в
`error.details.reason`; `request_id` / `correlation_id` совпадают с записью
аудита. Других конвертов отказа нет. Запрос, который не читается опубликованной
схемой (неизвестное поле, пропущенное поле, неверный тип), отказывается до
любого обработчика с `422 INVALID_REQUEST` и причиной `malformed_request`.

## Зависимости

| Компонент | Механизм | Операции |
|---|---|---|
| IS-001 `identity` (`>=0.3.0,<0.4.0`) | клиент над опубликованным API | `resolve_context` (только команда create-product) |
| IS-003 `authorization` (`>=0.1.0,<0.2.0`) | клиент над опубликованным API | `decide` |
| IS-005 `idempotency` (`>=0.1.0,<0.2.0`) | внутренняя consumer surface | `execute` (все команды изменения состояния) |

**Ни один модуль `commerce_service` не импортирует `authorization_service`,
`identity_service` или `tenant_authority`.** IS-005 — единственная прямая
зависимость-библиотека (guard): компонент не создаёт второй механизм
идемпотентности. IS-001 потребляется командой create-product, у которой ещё
нет цели: тенант вопроса авторизации может быть только эффективным тенантом
verified identity.

## Commerce ↔ Learning

Только опубликованные контракты — в обе стороны. Прямого доступа к чужой
БД, таблицам, очередям и внутренним модулям нет. Автоматического
`paid order → learning access` нет: это отдельное будущее решение через
опубликованный контракт. Learning в рамках Commerce MVP не изменяется.

## Данные компонента

| Набор | Область | Комментарий |
|---|---|---|
| `products` | `tenant-scoped` | ровно один владелец; ровно `ACTIVE`; тенант из verified identity |
| `offers` | `tenant-scoped` | ровно один Product того же тенанта; ровно `ACTIVE` |
| `prices` | `tenant-scoped` | ровно один Offer того же тенанта; append-only |
| `carts` | `tenant-scoped` | один покупатель; изменяемое состояние |
| `cart_lines` | `tenant-scoped` | одна строка на (Cart, Offer); количество положительное |
| `orders` | `tenant-scoped` | факты покупки неизменяемы; меняется только payment state |
| `order_lines` | `tenant-scoped` | снепшот цены; обновлений нет |
| `payment_state` | `tenant-scoped` | `PENDING_PAYMENT → PAID`; хранится полями Order |
| `access_audit` | `platform-scoped` | append-only журнал каждой попытки доступа |

## Тесты

| Файл | Что доказывает |
|---|---|
| `tests/test_commerce_domain.py` | модели и хранилище: цепочка Product→Offer→Price, сироты, чужой тенант, отсутствие `Product.price`, снепшот OrderLine, неизменяемость фактов покупки, изменяемость payment state, opaque buyer |
| `tests/test_commerce_skeleton.py` | каркас: сборка, health/ready, создание/чтение товара через цепочку, replay/conflict, изоляция, отказы, аудит, published client |
| `tests/test_commerce_contract.py` | conformance контракта к живой реализации |
| `tests/test_commerce_boundary.py` | отсутствие недопустимых cross-component импортов; клиент несёт только значения |
