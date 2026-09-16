# Каталог ADR

`docs/adr/` — единственный каталог архитектурных решений и архитектурной истории проекта.

## Правила нумерации

- Новые ADR получают следующий свободный последовательный номер.
- Существующие номера не переиспользуются.
- Отсутствующий исторический ADR не восстанавливается предположением.
- Наличие ссылки или упоминания номера не является доказательством существования документа.
- Исторические пробелы фиксируются в ближайшем подтверждённом ADR, если это необходимо для понимания текущего состояния.

## Текущее подтверждённое состояние

В репозитории подтверждены:

- `ADR-0010 — Аудит архитектурных пробелов: ARCHITECTURE.md 1.1.0 → 1.2.0` — `RATIFIED` (2026-09-08). Основание редакции ARCHITECTURE.md 1.2.0.
- `ADR-0011 — SCS-001 Learning Content Authoring Boundary` — `RATIFIED` (2026-09-10). Решение о первой срезе авторского контента SCS-001 (Issue #31). Исторический факт сохраняется: реализация среза попала в `main` (commit `8445e2b`) при статусе `PROPOSED`; отклонение от последовательности «ратифицировать → реализовать» зафиксировано в разделе Ratification самого ADR, а не переписано.
- `ADR-0012 — SCS-001 Student Enrollment Boundary` — `RATIFIED` (2026-09-10). Минимальная граница Student Enrollment для SCS-001: student self-enrollment в опубликованный Course, tenant isolation, authorization, idempotency и защита от дублирования; существующая семантика Course Read не изменяется.
- `ADR-0013 — Commerce Product Boundary` — `RATIFIED` (2026-09-11). Граница второго независимого Business System / SCS — Commerce (Issue #53): Class A, минимальный MVP-контур `Product → Offer → Price` и `Cart → Checkout → Order → OrderLine`; payment — изменяемое коммерческое состояние/результат, связанный с Order, при неизменяемых фактах покупки (без Payment Service); buyer через непрозрачную ссылку в существующую Identity; данные tenant-scoped; Commerce ↔ Learning — только через опубликованные контракты; `paid order → learning access` вынесен в отдельное будущее решение.
- `ADR-0014 — SCS-003 Booking Boundary` — `RATIFIED` (2026-09-13). Граница третьего независимого Business System / SCS — Booking (Issue #57): Class A, Level 0, `Resource → Availability Window → Reservation`, tenant isolation, UTC/half-open intervals, concurrent no-double-booking invariant, lifecycle `ACTIVE → CANCELLED`, независимость от Commerce и Learning.
- `ADR-0015 — Activate Level 2 Component Factory` — `RATIFIED` (2026-09-14). Активирует Level 2 — Component Factory после прохождения Factory Gate #1, при сохранении существующих бизнес-границ, владения данными, контрактных и compatibility-правил. Реализация фабричных механизмов выполняется инкрементально отдельными implementation slices: Slice A — Component Registry, Slice B — Component Catalog, Slice C — Platform Manifest, Slice D — Golden Bundles, Slice E — Composer и Slice F — Platform Instance Assembly реализованы (Slice F — Issue #74, PR #75 merged 2026-09-16); deployment/provisioning/rollout-исполнение поверх собранного Platform Instance требует отдельного утверждённого work item.

`ADR-0009` не существует в подтверждённой истории и намеренно не реконструируется. Сведения о более ранних решениях используются только там, где они подтверждены сохранившимися артефактами.

## Каноническое правило

`ARCHITECTURE.md` является действующим техническим законом. ADR фиксируют решения, изменения и их происхождение; они не создают конкурирующую архитектурную конституцию.
