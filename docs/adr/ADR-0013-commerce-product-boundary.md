# ADR-0013 — Commerce Product Boundary

**Status:** `RATIFIED` — принято владельцем проекта 2026-09-11
**Date:** 2026-09-11
**Ratified:** 2026-09-11
**Level:** D — domain/architecture boundary decision
**Initiated by:** Issue #53

> This ADR fixes a business boundary. It implements nothing: no Commerce code,
> no migrations, no API, no UI, and no changes to Learning, Identity, Tenant
> Authority or Authorization are authorized by this document. Implementation
> may be planned only after architectural review and owner ratification.

## Context

The project currently has exactly one Business System: SCS-001 Learning, a
Class A component owning the learning chain
`Course → Module → Lesson → Assignment → Submission → Teacher Review`
(ADR-0011) plus Student Enrollment (ADR-0012). Learning publishes Courses but
has no commercial capability; "payment" is an explicit non-goal of ADR-0012.

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED already names **Commerce** as an example
of **Class A — Business System / SCS** (§3) and as a possible vertical profile
(§23). The law permits a second business system; it does not yet define one.
This ADR exercises the existing classification and defines, before any
implementation, what Commerce is, what it owns, and what it must not do.

This follows the project's established order of work (§32 ARCHITECTURE.md:
business capability and data owner first; LAW-14: boundaries change through
ADR) and the process rule recorded in the ADR-0011 ratification note: essential
component decisions are ratified before their implementation is merged.

The repository remains in Level 0 — Modular Monolith, standalone/foundation
mode (§4.1): 7 components, in-memory stores, Component Contracts 7/7, Quality
Gate green. Nothing in this ADR changes that state.

## Decision

1. **Commerce is Class A — Business System / SCS**: a self-contained business
   product with its own UI, its own data and its own lifecycle — the second
   independent business product after Learning.
2. **Commerce is not part of Learning.** It has its own business boundary and
   owns commercial facts (what is for sale, at what price, who selected what,
   who bought what, and whether it is paid).
3. The initial Commerce boundary is the minimal MVP contour defined below:
   `Product → Offer → Price` and `Cart → Checkout → Order → OrderLine`, plus
   payment state, buyer identity reference, tenant isolation, authorization,
   idempotency and audit.
4. **Payment in this MVP is a commercial state/result of an Order, not a
   service.** No Payment Service is created and no payment provider is
   integrated.
5. Commerce data are `tenant-scoped` and Commerce uses the existing platform
   mechanisms for identity, tenant context, authorization, idempotency and
   audit. No new foundation service of any kind is created.
6. `paid order → learning access` is **not** part of this boundary; it is a
   separate future cross-component decision made through a published contract.
7. The component identifier and public Component Contract of Commerce are
   established when the component itself is created (§2.4, §6.1, §11
   ARCHITECTURE.md), after ratification of this ADR. This document deliberately
   assigns neither.

## Commerce Business Boundary

The minimal MVP contour:

```text
Product
   ↓
Offer
   ↓
Price

Cart
   ↓
Checkout
   ↓
Order
   ↓
OrderLine
```

Plus: payment state, buyer identity reference, tenant isolation,
authorization, idempotency, audit.

Commerce owns:

- the catalog of what is sold: `products`, `offers`, `prices`;
- the buying process state: `carts`;
- the commercial facts: `orders`, `order_lines`, and the payment state of an
  Order;
- an audit record of security-sensitive and state-changing operations.

Commerce does not own and must not absorb:

- learning content, enrollment or learning access (owner: Learning);
- identity, credentials or user profiles (owner: Identity);
- the tenant registry and tenant lifecycle (owner: Tenant Authority);
- permissions and authorization decisions (owner: Authorization);
- generic resource ownership semantics beyond its own boundary (IS-004
  remains the Data Ownership / Resource Boundary).

The boundary diagram for the future second SCS:

```text
        COMMERCE (Class A — Business System / SCS)
        owns: products, offers, prices, carts,
              orders, order lines, payment state
            │
            │ published contract only (API / events / export)
            ▼
   Identity · Tenant Authority · Authorization · Idempotency   (foundation)
            │
            │ published contract only
            ▼
        LEARNING (Class A — Business System / SCS)
```

Both business systems consume the same foundation boundaries; neither reaches
into the other's data or internal modules.

## Business Semantics

Each MVP object has one business meaning. The semantics below are business
definitions; field lists, storage layout and exact API schemas are
implementation-level decisions that must remain aligned with the future
Component Contract and OpenAPI.

### Product / Offer / Price

**Product** — a commercially sellable object in the tenant's catalog.

- A Product is *what can be offered for sale*. Commerce owns the fact of its
  commercial existence, not the nature of the delivered value.
- **Product is domain-agnostic.** It must not contain or reference Learning
  internals: no Course, Lesson, Assignment, Enrollment, Submission or Review
  fields, no foreign keys into the `learning` schema, no imports of
  `learning_service` modules. If a Product must be associated with an object
  of another business component, that association exists only as an opaque
  identifier interpreted through a published contract of that component —
  never as structural coupling.
- No universal integration registry or catalog of "product kinds" is
  introduced by this ADR.

**Offer** — a sellable proposition of a Product.

- The Offer is the object a buyer actually selects: it links exactly one
  Product to a sellable proposition within the tenant.
- Product and Offer are distinct concepts so that commercial selection does
  not have to mutate the catalog object itself.
- The exact lifecycle and fields of Offer are implementation-level decisions;
  they must not reintroduce a hidden `Product.price`.

**Price** — a separate first-class price concept attached to the sellable
proposition.

- The boundary is explicitly `Product → Offer → Price`. Collapsing it to
  `Product.price` is not allowed: a price belongs to a sellable proposition,
  not to the catalog object, and is a concept in its own right (it can be
  current, changed, or superseded over time).
- Symmetrically, no additional pricing machinery (price books, tiers, currency
  matrices, pricing engines) is created just to make the model look
  enterprise-grade. The MVP has exactly the three concepts above.
- The current Price is the price of *now*. The price of *a purchase* lives in
  the Order (see OrderLine snapshot below). Changing a current Price must
  never mutate a historical Order.

### Cart / Checkout / Order

**Cart** — mutable pre-order state of a buyer.

- A Cart holds the offers a buyer has selected but has not yet bought. It is
  tenant-scoped and bound to a `buyer_identity_id`.
- Unlike the Order — whose purchase facts are immutable once created — the
  Cart is *not* a commercial fact. It may change, be emptied, or be abandoned
  without any commercial consequence.
- Carts are working state of the buying process; their exact retention rules
  are implementation-level decisions.

**Checkout** — a business command/process, not an entity.

- Checkout is the operation that converts the selected contents of a Cart
  into an Order: it validates the selected offers and their current prices,
  applies tenant/authorization/idempotency rules, and produces the Order,
  fixing its immutable purchase facts at creation.
- **No persistent Checkout entity is created.** Checkout is a command with an
  effect (Order creation), not a long-lived object. Introducing a persistent
  checkout/document state would require a proven business need and a separate
  decision.

**Order** — the central commercial fact.

- An Order records the commercial facts of what a specific buyer bought in a
  specific tenant, at which captured prices. These **purchase facts — the
  buyer, the tenant, the bought positions (OrderLines), the captured prices
  and the other commercial facts of the purchase — are immutable after
  creation**.
- The **payment state** is deliberately not among those frozen facts: it is a
  mutable lifecycle/result associated with the Order and may change after
  creation (see Payment State below). Conceptually:

```text
Order
├── purchase facts — immutable after creation
└── payment state  — mutable lifecycle/result
```

- The Order is the authoritative commercial record of Commerce. Later changes
  to the catalog (Product, Offer, current Price) do not rewrite it.

**OrderLine** — one position of an Order.

- Each OrderLine captures one bought proposition with a **price snapshot at
  order time** (amount and, at minimum, enough reference to identify what was
  bought — via Product/Offer references that remain meaningful even if the
  current catalog changes).
- **Snapshot invariant:** a historical Order and its OrderLines are never
  recomputed from the current catalog. Renaming, repricing, or removing a
  Product/Offer/Price after purchase must not alter past Orders. Whether the
  underlying catalog entries are archived or kept is an implementation-level
  decision subordinated to this invariant.

## Payment State

This must be read as the strictest constraint of this ADR.

**Payment is not a Payment Service.** In the initial Commerce MVP, payment is
the commercial state/result of payment associated with an Order — a recorded
business fact owned by Commerce ("this order is unpaid / paid"), not a
separate component, process or integration surface.

Payment state is the **mutable part** of an Order. It may progress through a
payment lifecycle — for example from a not-yet-paid state to a paid result
(`PENDING_PAYMENT → PAID` as an illustration; the exact state set is an
implementation-level decision). A payment-state change alters only this
lifecycle/result; it never alters the immutable purchase facts of the Order.

Consequently, this ADR forbids, within the MVP:

- creating a `Payment Service` or any Payment Platform Service component
  (§3 lists "Payments" only as a *permissible* Class B example; the law does
  not mandate it, and LAW-13 — Complexity Must Be Earned — forbids creating
  one without a real problem to solve);
- integrating a real external payment provider;
- building an artificial imitation of a full payment gateway (a fake
  provider-facing API surface pretending to be an integration). Recording the
  commercial result of a payment inside an Order is Commerce business data
  and is in scope; simulating a gateway is not.

The exact storage model of payment state (fields on the Order versus a
separate owned record) is an implementation-level decision. This ADR
intentionally does not fix it: it fixes only that the payment state is
Commerce-owned commercial data bound to an Order, and that no new component
is created for it.

If, in the future, real payment processing becomes necessary, it changes
nothing retroactively: Orders and their payment states remain Commerce
business facts, and a dedicated payment component — if ever justified — would
require its own ADR under LAW-13/LAW-14.

## Buyer Identity

Commerce introduces **no second user, customer-profile or buyer service**.

- A buyer is referenced by `buyer_identity_id` — an **opaque reference** to
  the existing Identity (IS-001), exactly the pattern already used for
  `student_identity_id` in Learning. Commerce stores and uses the reference;
  it does not interpret, enrich or duplicate the identity behind it.
- Commerce must not copy internal Identity data (profiles, contacts,
  credentials) without an architectural necessity; any such necessity is a
  separate decision.
- **Authentication** remains the existing platform mechanism: Commerce never
  verifies credentials itself and deals only with verified identity and the
  effective tenant derived from it.
- **Authorization** remains the existing Authorization boundary (IS-003) with
  permissions evaluated at the Commerce ownership boundary.

## Tenant Isolation

All Commerce business data are **`tenant-scoped`** (LAW-16, LAW-16a, §5.4
ARCHITECTURE.md):

- `products`, `offers`, `prices`, `carts`, `orders`, `order_lines` and the
  payment state of an Order each belong to exactly one tenant and one data
  owner (`commerce`). The audit record follows the existing platform-wide
  pattern (`platform-scoped` append-only log).
- The effective tenant is always derived from the verified identity through
  the existing chain (IS-001 → Tenant Authority IS-002). A caller-supplied
  `tenant_id` is a cross-check only and is rejected on mismatch; it never
  selects the tenant of an operation.
- Cross-tenant reads and writes are rejected. Cross-tenant operations over
  Commerce data do not exist in the MVP.

Commerce creates **no own Tenant Service and no second tenant-isolation
mechanism**. Tenant Authority (IS-002) remains the single tenant lifecycle
authority.

## Commerce ↔ Learning Boundary

This is one of the principal decisions of this ADR.

The only permitted relationship is through **published contracts**:

```text
Commerce
    │
    │ published contract
    ▼
Learning
```

and never:

```text
Commerce → Learning DB           — forbidden
Commerce → Learning tables       — forbidden
Commerce → Learning internal Python modules — forbidden
```

Direct access is forbidden in both directions (LAW-04, §18 ARCHITECTURE.md):

- **Commerce does not own Learning data.** Commerce must not read or write
  Learning storage, internal tables, internal queues or internal modules.
- **Learning does not own Commerce data.** Learning must not read or write
  Commerce storage or internal modules.
- Each component remains the owner of its own business information. Any
  interaction between them occurs exclusively through a published, versioned
  contract of the owning component (API, events, or Data Export/CDC as
  declared in its Component Contract).

Until Learning publishes a contract that Commerce may consume, Commerce makes
no assumptions about Learning at all — including the existence of any
particular endpoint.

## Paid Order → Learning Access

**Not part of this ADR.** The MVP does not include automatic granting of
learning access upon purchase.

Architecturally, the separation of concerns is fixed as:

```text
Commerce: the commercial fact of purchase
Learning: the fact and the rules of learning access
```

A paid Order is proof that commerce happened. It is not, by itself, an
enrollment or an entitlement. The linkage

```text
paid order → learning access
```

is a separate cross-component business decision. When a real need arises, it
will be designed through a published contract between the two business
systems (consistent with §6.3/§6.4 on events and sagas — atomicity across
components is not assumed and must not be depicted in any UI), and it will
require its own ADR-level decision. This ADR introduces **no changes to
Learning** to prepare for that linkage, and no subscription, licensing or
entitlement model.

## Authorization / Idempotency / Audit

Commerce uses the existing fundamental capabilities, per their current
contracts. It creates no substitutes.

**Authorization** — every Commerce operation follows the platform enforcement
chain, in the same shape Learning already proves:

```text
Identity (IS-001) → effective tenant → Authorization (IS-003)
    → Commerce ownership boundary → owned-data operation
```

Each step can only refuse; the operation on data is executed only after the
chain passes, and exactly once (IS-005).

**Idempotency** — state-changing Commerce commands (cart mutations, checkout
and any catalog command) use the existing Idempotency boundary (IS-005) with
`Idempotency-Key`, per §6.5 ARCHITECTURE.md: a repeated request with the same
key produces no second effect; a key reuse with a different binding is a
conflict. Checkout is idempotent by construction: a replayed checkout does
not create a second Order.

**Audit** — security-sensitive and state-changing Commerce operations are
recorded through the existing audit semantics (append-only, `platform-scoped`
audit record, `request_id`/`correlation_id` linkage), as Learning already
does. At minimum, catalog changes, cart mutations, checkout results,
payment-state changes, and refusals caused by authorization, tenant boundary
or idempotency rules must remain observable through existing audit semantics.

**No new foundation services:** this ADR creates no new Idempotency Service,
no new Audit Service, no new Authorization Service, no new Identity Service
and no new Tenant Service. Commerce consumes IS-001, IS-002 (via IS-001),
IS-003 and IS-005 within their declared contracts.

## Data Ownership

Commerce will own its data under logical ownership (§5 ARCHITECTURE.md), the
same regime as the existing components at Level 0. Declared datasets and
scopes:

| Dataset | Scope | Comment |
|---|---|---|
| `products` | `tenant-scoped` | catalog of sellable objects; domain-agnostic |
| `offers` | `tenant-scoped` | sellable propositions; exactly one Product of the same tenant |
| `prices` | `tenant-scoped` | price concept of a sellable proposition; current price may change, history preserved per snapshot rules |
| `carts` | `tenant-scoped` | mutable pre-order state of one buyer |
| `orders` | `tenant-scoped` | central commercial fact; its purchase facts (buyer, tenant, positions, captured prices) are immutable after creation |
| `order_lines` | `tenant-scoped` | positions with an immutable price snapshot at order time (purchase facts) |
| payment state | `tenant-scoped` | mutable lifecycle/result associated with an Order; changes never touch the Order's immutable purchase facts (storage form is an implementation-level decision) |
| commerce audit | `platform-scoped` | append-only record, same pattern as existing components |

The logical schema and owner name (`commerce`) are fixed at component
creation in the Component Contract; physical layout (§5.1: logical schemas,
roles, RLS as needed) is an implementation-level decision. No other component
gains any right of direct access to these datasets (LAW-03, LAW-04).

## API / Contract Boundary

This ADR creates **no Component Contract and no OpenAPI document**. When the
component is created after ratification, its public surface must satisfy:

- a machine-readable Component Contract (§6.1) declaring `api`, `events`,
  `data_export_cdc`, `ui`, `configuration_schema`, `data_ownership`, `authn`,
  `authz`, `compatibility_policy`, `dependencies`, with data scopes as tabled
  above; undeclared surface is internal and unprotected by compatibility;
- **business commands, not generic CRUD** — the surface expresses commerce
  operations (create product, define offer, set price, mutate cart, checkout,
  read order), in the style of the existing SCS-001 command/read operations;
- versioned API per §6.6/§7 (e.g. `/api/v1/commerce/...`), OpenAPI-described,
  contract-tested per §10;
- no events/CDC introduced solely for this boundary in the MVP; if and when
  Commerce publishes events, they follow §6.3/§6.8 (CloudEvents envelope,
  tenant context, schema versioning) and the Component Contract.

## Compatibility

This ADR is additive and compatible with `docs/ARCHITECTURE.md` v1.2.0
RATIFIED:

- **Ownership boundaries** — unchanged; Commerce adds one more owner, it does
  not move any existing ownership (§5.3 stays literally true: Learning owns
  Learning data; Commerce will own Commerce data).
- **Tenant-scoped data** — all new business data declared `tenant-scoped`;
  LAW-16/LAW-16a unchanged; tenant context derivation unchanged.
- **Component Contract** — no existing contract changes; the future Commerce
  contract follows §6.1. Existing 7 contracts remain 7/7 valid.
- **Published contracts** — the Commerce ↔ Learning relationship is defined
  as contract-only from day one; no consumer of any existing contract breaks.
- **Authentication / authorization** — existing mechanisms only; IS-001 and
  IS-003 boundaries unchanged.
- **Idempotency / audit** — existing mechanisms only (IS-005 and the
  established audit pattern); §6.5 unchanged.
- **No direct DB access** — nothing in this decision requires or permits
  cross-component storage access (LAW-04, §18).
- **No microservice for architecture's sake** — Commerce remains one
  component at Level 0 (LAW-12, LAW-13); no decomposition is proposed.
- **Level 0 and factory gates** — the project stays in standalone/foundation
  mode (§4.1). This ADR introduces no Component Catalog, no Component
  Registry, no Composer, no Golden Bundles, no release trains, and does not
  move the project to Level 1/2/3.

No architecture version bump is proposed: this decision operates entirely
within existing law.

## Explicit Non-Goals

This ADR does not authorize, and the Commerce MVP must not include:

- a real payment provider or any external payment integration;
- a separate Payment Service / payment component;
- refunds;
- promo codes;
- discount campaigns;
- subscriptions;
- loyalty;
- marketplace (multi-vendor commerce);
- CRM;
- warehouse / inventory;
- shipping / delivery;
- analytics;
- reporting;
- marketing automation;
- Learning internals (any dependence on Course, Lesson, Assignment,
  Enrollment, Submission, Review or Learning tables);
- direct database access to another component's data;
- new Identity / Tenant / Auth foundation services (including a second
  customer-profile service);
- Component Catalog;
- Component Registry;
- Composer;
- Golden Bundles;
- microservices;
- generic CRUD replacing business commands.

No capability is added to make Commerce "look like a complete shop". Each of
the above requires its own demonstrated need and its own decision.

## Acceptance Criteria

A future Commerce implementation is conformant with this ADR only if:

1. Commerce exists as a separate Class A — Business System / SCS component;
   it is not a module, schema or feature of Learning.
2. The MVP surface realizes exactly the contour `Product → Offer → Price`,
   `Cart → Checkout → Order → OrderLine` plus payment state, buyer identity
   reference, tenant isolation, authorization, idempotency and audit.
3. Product, Offer and Price are distinct concepts; there is no hidden
   `Product.price` and no extra pricing machinery.
4. Cart is mutable pre-order state; Checkout is a command/process; no
   persistent Checkout entity exists.
5. Order purchase facts (buyer, tenant, positions, captured prices) are
   immutable after Order creation; OrderLine purchase facts and their price
   snapshots are immutable; the payment state may change after creation as a
   mutable lifecycle/result associated with the Order.
6. Changing a current Price (or Product/Offer) never mutates the purchase
   facts of a historical Order; the current catalog price never overwrites a
   historical OrderLine price snapshot.
7. Payment is a Commerce-owned state/result associated with an Order; it is
   mutable and independent of the Order's immutable purchase facts; no
   Payment Service, no payment provider integration, no gateway imitation
   exists.
8. Buyer is referenced by an opaque `buyer_identity_id` into the existing
   Identity; no second user/customer-profile service and no copied identity
   internals exist.
9. All Commerce business datasets are `tenant-scoped`; the effective tenant
   is derived from verified identity; caller-supplied `tenant_id` is
   cross-check only; cross-tenant operations are rejected.
10. Authorization runs through the existing chain (IS-001 → IS-003 → Commerce
    ownership boundary); no second authorization mechanism exists.
11. State-changing commands require `Idempotency-Key` and are executed via
    IS-005; replayed checkout never creates a second Order.
12. Security-sensitive operations and refusals are observable through the
    existing audit semantics.
13. Commerce accesses Learning (and everything else) only through published
    contracts; no Learning DB/table/module access in either direction.
14. Product is domain-agnostic: no Learning entities or tables are referenced
    structurally by the Commerce model.
15. No automatic `paid order → learning access` exists; Learning is unchanged
    by the Commerce implementation.
16. No new foundation service (Identity/Tenant/Auth/Idempotency/Audit) is
    created.
17. No explicit non-goal is implemented as part of the MVP.
18. The Component Contract and OpenAPI (when created) declare the data
    scopes, authn/authz, compatibility policy and dependencies listed here.
19. The project remains Level 0, standalone mode; no factory mechanics are
    introduced; the existing Quality Gate remains green.

## Alternatives Considered

- **Commerce as part of Learning.** Rejected: it would make one business
  system the owner of another's facts, violate LAW-01/LAW-03, and couple the
  learning lifecycle to commerce. The law already classifies Commerce as its
  own Class A system.
- **Payment Service now (Class B).** Rejected: §3 lists "Payments" as a
  permissible example, not a mandate; no real processing need exists at MVP;
  LAW-13 forbids earning complexity nobody needs. A future dedicated payment
  component remains possible through its own ADR.
- **`Product.price` instead of Product → Offer → Price.** Rejected: it
  destroys the ratified boundary contour and conflates the catalog object
  with its sellable proposition and price.
- **Full pricing model (price books, tiers, currencies, pricing engine).**
  Rejected for the MVP: no less artificial than `Product.price`; complexity
  must be earned (LAW-13).
- **Persistent Checkout entity.** Rejected without a proven need: checkout is
  a process producing an Order, not a business fact of its own.
- **Rich buyer profile in Commerce.** Rejected: duplicates Identity (LAW-03);
  an opaque `buyer_identity_id` reference is sufficient.
- **Product with a typed link to Course (commerce-of-learning in the
  model).** Rejected: it makes Product Learning-dependent and preempt the
  `paid order → learning access` decision. An opaque identifier via a
  published contract is the reversible, boundary-preserving option (LAW-15).
- **Automatic learning access on paid Order in the MVP.** Rejected: it is a
  cross-component business process requiring its own published contract and
  decision; baking it in now would couple two SCSs prematurely.
- **No ADR until implementation.** Rejected: contradicts the project's
  ratified-before-implemented rule recorded in ADR-0011's ratification note.

## Consequences

**Positive.** The second business system gets an explicit, reviewable
boundary before any code exists; commercial facts get exactly one owner; the
Commerce ↔ Learning relationship starts contract-only, which keeps both
systems independently evolvable (LAW-06) and reversible (LAW-15); the
payment constraint prevents an unearned platform service from appearing.

**Negative / cost.** Until a future decision adds it, Commerce cannot take
real payments, refund, discount or grant learning access; some near-term
duplication of patterns (contract, tests, README) will exist because a second
Class A component is a real new component, not a reuse of Learning.

**Neutral.** The decision changes no existing behavior, contract or test; all
existing consumers are unaffected.

## Migration

This ADR migrates nothing: it changes no code, no schema, no contract and no
document other than itself.

Adoption path:

```text
ADR-0013 PROPOSED (this document)
    → architectural review (ChatGPT) — completed: one mandatory semantic
      correction (immutable Order purchase facts vs mutable payment state),
      then APPROVE
    → owner ratification — completed 2026-09-11
    → separate implementation Issue(s) for the Commerce MVP slice(s)
    → component creation: Component Contract + OpenAPI first,
      then implementation, contract tests and Quality Gate
```

A future implementation follows the existing Level 0 pattern (in-memory
store, logical ownership) and, when persistent storage is introduced,
`EXPAND → MIGRATE → CONTRACT` (§12). No factory mechanics are enabled by this
migration path.

## Architectural Authority

`docs/ARCHITECTURE.md` v1.2.0 RATIFIED remains the sole architectural law.
This ADR operates within it and changes none of it: LAW-01…LAW-16a, §3
(component classes), §5 (data ownership and scopes), §6 (contracts,
idempotency, events), §18 (dependency graph), §32 (developer rules) apply to
Commerce as to every component.

The existing foundation boundaries remain authoritative and unchanged:
IS-001 (Identity / Tenant Context), IS-002 (Tenant Authority), IS-003
(Authorization), IS-004 (Data Ownership / Resource Boundary), IS-005
(Idempotency), IS-006 (Saga).

ADR-0010, ADR-0011 and ADR-0012 remain in force; ADR-0012's non-goal
"payment" for Learning is unchanged by this ADR — commerce enters the
project as its own system, not as a Learning feature.

No architecture version bump is proposed unless the architectural review
identifies a genuine conflict with ratified law.

## Ratification

RATIFIED by the project owner (`@Kvasha62`) on 2026-09-11.

The ratification follows the completed architectural review: the initial
ChatGPT review required one semantic correction — the separation of the
immutable purchase facts of an Order from the mutable payment state
associated with it — the correction was applied, and the architectural
re-review returned APPROVE.

This ratification confirms the decision as written. It does not change
`docs/ARCHITECTURE.md` v1.2.0 RATIFIED, does not bump the architecture
version, and does not authorize any implementation by itself: implementation
work on Commerce requires a separate Issue, per the adoption path above and
the project's operating model.
