"""Owned data of the Learning component (logical schema `learning`).

This module is internal. No other component has direct access to it:
assignments, submissions and the audit journal are reachable only through
the published contract of this component (ARCHITECTURE.md §1.1, LAW-04).

The store separates the two ways its data is touched, and the separation
is the enforcement story of the component:

* :meth:`LearningStore.ownership_of_assignment`,
  :meth:`LearningStore.ownership_of_submission`,
  :meth:`LearningStore.ownership_of_course`,
  :meth:`LearningStore.ownership_of_module` and
  :meth:`LearningStore.ownership_of_lesson` — enforcement metadata:
  whether a record exists here and who its single owner is. The engine
  reads them to form the question it asks IS-003; they serve nothing to a
  caller;
* :meth:`LearningStore.serve_submission`,
  :meth:`LearningStore.list_submissions_for_assignment`,
  :meth:`LearningStore.apply_review`,
  :meth:`LearningStore.create_course`,
  :meth:`LearningStore.create_module`,
  :meth:`LearningStore.create_lesson`,
  :meth:`LearningStore.create_assignment`,
  :meth:`LearningStore.publish_course`,
  :meth:`LearningStore.archive_course` and
  :meth:`LearningStore.course_hierarchy` — the owned-data operations
  themselves. The engine calls them only after the enforcement chain
  produced an ``ALLOW``, and the tests count exactly these calls to prove
  that a denied request never reaches them.

Every mutation of one Course hierarchy — the three child creations,
publication and archive — executes inside the per-Course domain critical
section (:meth:`LearningStore.hierarchy_section`): the whole critical
sequence of a command (validation → state/parent/tenant/ownership checks →
write) is serialized against every other mutation of the same tree, so a
concurrent child creation can never interleave between a publication's
validation and its writes (Issue #31). This is Level-0 domain
serialization inside this component, not a second idempotency mechanism:
IS-005 stays exactly-once per key, while the section makes the atomic
business operation exclusive across different keys.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from learning_service import OWNER_COMPONENT
from learning_service.models import (
    AccessAuditEvent,
    OwnedAssignment,
    OwnedCourse,
    OwnedLesson,
    OwnedModule,
    OwnedSubmission,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


#: Submission lifecycle of this slice — exactly these two states. No
#: grading/review states exist.
SUBMISSION_STATES: tuple[str, ...] = ("DRAFT", "SUBMITTED")

#: Minimal Assignment lifecycle vocabulary of this slice.
ASSIGNMENT_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")

#: Content Authoring lifecycles (Issue #31): exactly these states, no
#: UNPUBLISH and no UNARCHIVE transition exists anywhere in the hierarchy.
COURSE_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")
MODULE_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")
LESSON_STATES: tuple[str, ...] = ("DRAFT", "PUBLISHED", "ARCHIVED")


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Three cases: the review command does not apply to the submission's
    current state (only ``SUBMITTED`` may be reviewed), the review fact
    is immutable (an already reviewed submission cannot be reviewed again),
    and the content-authoring state rules (a child is created only under a
    ``DRAFT`` parent; publication applies only to a structurally valid
    ``DRAFT`` hierarchy; archive applies only to a ``PUBLISHED`` hierarchy).
    It is raised only inside the owned-data step of the chain and is
    audited like every refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class LearningStore:
    """In-memory owned storage of the Learning data and the audit journal.

    The store owns every record of the Learning logical schema: courses,
    modules, lessons, assignments, submissions and the access audit journal.
    The hierarchy invariants (Issue #31) are facts of this store, not claims
    of a caller: every child is registered under an existing parent of the
    same Tenant, or the registration is refused outright.
    """

    assignments: dict[str, OwnedAssignment] = field(default_factory=dict)
    submissions: dict[str, OwnedSubmission] = field(default_factory=dict)
    courses: dict[str, OwnedCourse] = field(default_factory=dict)
    modules: dict[str, OwnedModule] = field(default_factory=dict)
    lessons: dict[str, OwnedLesson] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    #: One domain critical section per Course hierarchy (Issue #31). The
    #: locks are reentrant, so a Course command may hold the section across
    #: the transition and its recorded snapshot while the store methods
    #: re-acquire it inside.
    _hierarchy_locks: dict[str, threading.RLock] = field(
        default_factory=dict, repr=False, compare=False
    )
    _hierarchy_locks_guard: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # ------------------------------------------------------------------ static
    @staticmethod
    def _text(value: object, *, what: str, max_length: int) -> str:
        """Validate one content string of an authored record."""
        if not isinstance(value, str):
            raise TypeError(f"{what} must be a string")
        if not value.strip():
            raise ValueError(f"{what} must not be empty")
        if len(value) > max_length:
            raise ValueError(f"{what} exceeds the maximum length of {max_length}")
        return value

    @staticmethod
    def _position(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("position must be an integer")
        if value < 0:
            raise ValueError("position must not be negative")
        return value

    # ------------------------------------------------- the critical section
    def _course_lock(self, course_id: str) -> threading.RLock:
        """The one lock of one Course hierarchy (created on first use)."""
        with self._hierarchy_locks_guard:
            lock = self._hierarchy_locks.get(course_id)
            if lock is None:
                lock = threading.RLock()
                self._hierarchy_locks[course_id] = lock
            return lock

    @contextmanager
    def hierarchy_section(self, course_id: str) -> Iterator[None]:
        """The Level-0 domain critical section of one Course hierarchy.

        Every mutation of the tree — create-module, create-lesson,
        create-assignment, publish, archive — holds this section across its
        whole critical sequence: validation → state/parent/tenant/ownership
        checks → write. A child creation therefore cannot interleave
        between a publication's validation and its writes, and the tree can
        never end up ``PUBLISHED`` with a ``DRAFT`` descendant (or archived
        with an unarchived one) as the result of concurrent commands. The
        checks that decide the outcome are re-evaluated inside the section,
        so no observation made before entering it is load-bearing.

        This is domain-level serialization of the business operation inside
        this component — not an idempotency mechanism and not a replacement
        for IS-005: the guard stays exactly-once per ``Idempotency-Key``
        (replay, conflict, binding), while the section makes the atomic
        hierarchy mutation exclusive across different keys. A thread that
        mutates at most one hierarchy at a time can never deadlock: every
        command takes exactly one course lock, always as the innermost one.

        Reentrant: the effect of a Course-level command holds the section
        across the transition and its recorded snapshot while the store
        methods re-acquire it on the same thread.
        """
        with self._course_lock(course_id):
            yield

    # ----------------------------------------------------------- registration
    def register_course(self, course: OwnedCourse) -> OwnedCourse:
        """Register one owned course; ownership is singular and explicit."""
        owner = course.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a course must have exactly one data owner")
        if not course.course_id or not str(course.course_id).strip():
            raise ValueError("a course must have a course_id")
        if not course.tenant_id or not str(course.tenant_id).strip():
            raise ValueError("a course must belong to exactly one tenant")
        if course.status not in COURSE_STATES:
            raise ValueError(f"unknown course status {course.status!r}")
        self._text(course.title, what="a course title", max_length=512)
        if not isinstance(course.description, str) or len(course.description) > 4096:
            raise ValueError(
                "a course description must be a string of at most 4096 chars"
            )
        if not course.created_by or not str(course.created_by).strip():
            raise ValueError("a course must record the identity that created it")
        if course.course_id in self.courses:
            raise ValueError(
                f"course {course.course_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        self.courses[course.course_id] = course
        return course

    def register_module(self, module: OwnedModule) -> OwnedModule:
        """Register one owned module under its Course.

        The parent Course must already be registered and a module cannot
        belong to another Tenant than its Course (no cross-tenant hierarchy,
        no orphan modules). A module is registered only in ``DRAFT``:
        publication and archive are Course-level effects, never registrations.
        """
        owner = module.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a module must have exactly one data owner")
        if not module.module_id or not str(module.module_id).strip():
            raise ValueError("a module must have a module_id")
        if module.status not in MODULE_STATES:
            raise ValueError(f"unknown module status {module.status!r}")
        if module.status != "DRAFT":
            # A module is registered only in DRAFT: publication and archive
            # are Course-level effects, never registrations.
            raise ValueError("a module is registered only in DRAFT")
        if module.module_id in self.modules:
            raise ValueError(
                f"module {module.module_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        self._text(module.title, what="a module title", max_length=512)
        self._position(module.position)
        parent = self.courses.get(module.course_id)
        if parent is None:
            raise ValueError(
                f"course {module.course_id!r} is unknown: "
                "a module needs a registered parent course"
            )
        if module.tenant_id != parent.tenant_id:
            raise ValueError("a module must belong to the same tenant as its course")
        self.modules[module.module_id] = module
        return module

    def register_lesson(self, lesson: OwnedLesson) -> OwnedLesson:
        """Register one owned lesson under its Module.

        The parent Module must already be registered and a lesson cannot
        belong to another Tenant than its Module (no cross-tenant hierarchy,
        no orphan lessons). A lesson is registered only in ``DRAFT``.
        """
        owner = lesson.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a lesson must have exactly one data owner")
        if not lesson.lesson_id or not str(lesson.lesson_id).strip():
            raise ValueError("a lesson must have a lesson_id")
        if lesson.status not in LESSON_STATES:
            raise ValueError(f"unknown lesson status {lesson.status!r}")
        if lesson.status != "DRAFT":
            # A lesson is registered only in DRAFT, like a module.
            raise ValueError("a lesson is registered only in DRAFT")
        if lesson.lesson_id in self.lessons:
            raise ValueError(
                f"lesson {lesson.lesson_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        self._text(lesson.title, what="a lesson title", max_length=512)
        if not isinstance(lesson.content, str) or len(lesson.content) > 65536:
            raise ValueError("a lesson content must be a string of at most 65536 chars")
        self._position(lesson.position)
        parent = self.modules.get(lesson.module_id)
        if parent is None:
            raise ValueError(
                f"module {lesson.module_id!r} is unknown: "
                "a lesson needs a registered parent module"
            )
        if lesson.tenant_id != parent.tenant_id:
            raise ValueError("a lesson must belong to the same tenant as its module")
        self.lessons[lesson.lesson_id] = lesson
        return lesson

    # -------------------------------------------------------------- ownership
    def register_assignment(self, assignment: OwnedAssignment) -> OwnedAssignment:
        """Register one owned assignment; ownership is singular and explicit.

        An assignment anchored in the content hierarchy (``lesson_id`` set)
        must have a registered parent Lesson of the same Tenant: no orphan
        and no cross-tenant assignments. A ``None`` ``lesson_id`` is the mark
        of records that predate Content Authoring (demo data); the submission
        flow semantics of such records are unchanged.
        """
        owner = assignment.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("an assignment must have exactly one data owner")
        if not assignment.assignment_id or not str(assignment.assignment_id).strip():
            raise ValueError("an assignment must have an assignment_id")
        if not assignment.tenant_id or not str(assignment.tenant_id).strip():
            raise ValueError("an assignment must belong to exactly one tenant")
        if assignment.status not in ASSIGNMENT_STATES:
            raise ValueError(f"unknown assignment status {assignment.status!r}")
        if assignment.assignment_id in self.assignments:
            raise ValueError(
                f"assignment {assignment.assignment_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        if assignment.lesson_id is not None:
            parent = self.lessons.get(assignment.lesson_id)
            if parent is None:
                raise ValueError(
                    f"lesson {assignment.lesson_id!r} is unknown: "
                    "an anchored assignment needs a registered parent lesson"
                )
            if assignment.tenant_id != parent.tenant_id:
                raise ValueError(
                    "an assignment must belong to the same tenant as its lesson"
                )
            self._text(
                assignment.title or "", what="an assignment title", max_length=512
            )
            if (
                not isinstance(assignment.instructions, str)
                or len(assignment.instructions) > 4096
            ):
                raise ValueError(
                    "assignment instructions must be a string of at most 4096 chars"
                )
        self.assignments[assignment.assignment_id] = assignment
        return assignment

    def register_submission(self, submission: OwnedSubmission) -> OwnedSubmission:
        """Register one owned submission under its assignment.

        The submission's tenant must equal its assignment's tenant: a
        submission cannot belong to another Tenant than its Assignment.
        The assignment must already be registered — a submission without a
        known parent is refused outright.
        """
        owner = submission.owner_component
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("a submission must have exactly one data owner")
        if not submission.submission_id or not str(submission.submission_id).strip():
            raise ValueError("a submission must have a submission_id")
        if submission.status not in SUBMISSION_STATES:
            raise ValueError(f"unknown submission status {submission.status!r}")
        if submission.submission_id in self.submissions:
            raise ValueError(
                f"submission {submission.submission_id!r} is already owned: "
                "ownership is singular and cannot be redefined"
            )
        parent = self.assignments.get(submission.assignment_id)
        if parent is None:
            raise ValueError(
                f"assignment {submission.assignment_id!r} is unknown: "
                "a submission needs a registered parent"
            )
        if submission.tenant_id != parent.tenant_id:
            raise ValueError(
                "a submission must belong to the same tenant as its assignment"
            )
        if not isinstance(submission.attempt, int) or submission.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not isinstance(submission.content, dict):
            raise TypeError("content must be an object")
        self.submissions[submission.submission_id] = submission
        return submission

    def ownership_of_assignment(self, assignment_id: str) -> OwnedAssignment | None:
        """Enforcement metadata for an assignment: existence and owner/tenant.

        Reading this is part of forming the authorization question, not an
        owned-data operation.
        """
        return self.assignments.get(assignment_id)

    def ownership_of_submission(self, submission_id: str) -> OwnedSubmission | None:
        """Enforcement metadata for a submission: existence and owner/tenant."""
        return self.submissions.get(submission_id)

    def ownership_of_course(self, course_id: str) -> OwnedCourse | None:
        """Enforcement metadata for a course: existence and owner/tenant."""
        return self.courses.get(course_id)

    def ownership_of_module(self, module_id: str) -> OwnedModule | None:
        """Enforcement metadata for a module: existence and owner/tenant."""
        return self.modules.get(module_id)

    def ownership_of_lesson(self, lesson_id: str) -> OwnedLesson | None:
        """Enforcement metadata for a lesson: existence and owner/tenant."""
        return self.lessons.get(lesson_id)

    # -------------------------------------------------- authoring owned data
    def create_course(
        self,
        *,
        tenant_id: str,
        title: str,
        description: str,
        created_by: str,
        now: str,
    ) -> OwnedCourse:
        """The owned-data creation effect of the create-course command.

        The Tenant is the effective tenant of the verified identity as stated
        by the enforcement chain — never a caller claim. A new course is
        created in ``DRAFT``; nothing else in the store is touched.
        """
        return self.register_course(
            OwnedCourse(
                course_id=_new_id("crs"),
                tenant_id=tenant_id,
                owner_component=OWNER_COMPONENT,
                title=title,
                description=description,
                status="DRAFT",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
        )

    def create_module(
        self, course_id: str, *, title: str, position: int, now: str
    ) -> OwnedModule:
        """The owned-data creation effect of the create-module command.

        The state rule of the command lives here, inside the effect: only a
        ``DRAFT`` Course accepts new modules — a published or archived
        hierarchy is immutable. The module belongs to the Course's Tenant by
        construction and is created in ``DRAFT``.

        The whole critical sequence runs inside the Course's domain critical
        section, so a concurrent publication or archive cannot interleave.
        """
        with self.hierarchy_section(course_id):
            course = self.courses[course_id]
            if course.status != "DRAFT":
                raise DomainRefusal("invalid_state_transition")
            return self.register_module(
                OwnedModule(
                    module_id=_new_id("mod"),
                    course_id=course_id,
                    tenant_id=course.tenant_id,
                    owner_component=OWNER_COMPONENT,
                    title=title,
                    position=position,
                    status="DRAFT",
                    created_at=now,
                    updated_at=now,
                )
            )

    def create_lesson(
        self, module_id: str, *, title: str, content: str, position: int, now: str
    ) -> OwnedLesson:
        """The owned-data creation effect of the create-lesson command.

        Only a ``DRAFT`` Module accepts new lessons; the lesson belongs to
        the Module's Tenant by construction and is created in ``DRAFT``.

        The whole critical sequence runs inside the owning Course's domain
        critical section (a Module belongs to exactly one Course, so the
        mapping selects the section); the Module is re-read inside, because
        its state may have changed while this command waited for it.
        """
        module = self.modules[module_id]
        with self.hierarchy_section(module.course_id):
            module = self.modules[module_id]
            if module.status != "DRAFT":
                raise DomainRefusal("invalid_state_transition")
            return self.register_lesson(
                OwnedLesson(
                    lesson_id=_new_id("les"),
                    module_id=module_id,
                    tenant_id=module.tenant_id,
                    owner_component=OWNER_COMPONENT,
                    title=title,
                    content=content,
                    position=position,
                    status="DRAFT",
                    created_at=now,
                    updated_at=now,
                )
            )

    def create_assignment(
        self, lesson_id: str, *, title: str, instructions: str, now: str
    ) -> OwnedAssignment:
        """The owned-data creation effect of the create-assignment command.

        The existing Assignment model is extended, not replaced: the new
        assignment carries the parent Lesson, the lesson's Tenant, the
        title/instructions content and the ``DRAFT`` status. Only a ``DRAFT``
        Lesson accepts new assignments.

        The whole critical sequence runs inside the owning Course's domain
        critical section (selected through the registered parent chain); the
        Lesson is re-read inside, because its state may have changed while
        this command waited for it.
        """
        lesson = self.lessons[lesson_id]
        module = self.modules[lesson.module_id]
        with self.hierarchy_section(module.course_id):
            lesson = self.lessons[lesson_id]
            if lesson.status != "DRAFT":
                raise DomainRefusal("invalid_state_transition")
            return self.register_assignment(
                OwnedAssignment(
                    assignment_id=_new_id("asg"),
                    tenant_id=lesson.tenant_id,
                    owner_component=OWNER_COMPONENT,
                    status="DRAFT",
                    created_at=now,
                    updated_at=now,
                    lesson_id=lesson_id,
                    title=title,
                    instructions=instructions,
                )
            )

    # ------------------------------------------------ publication and archive
    def _hierarchy_of_course(
        self, course: OwnedCourse
    ) -> tuple[list[OwnedModule], list[OwnedLesson], list[OwnedAssignment]]:
        """The descendants of one Course, in hierarchy order.

        A record belongs to the hierarchy only through its registered parent
        chain: modules of the Course, lessons of those modules, assignments
        anchored to those lessons. Records outside the chain (e.g. the demo
        assignments that predate authoring) are not part of any hierarchy.
        """
        modules = [
            module
            for module in self.modules.values()
            if module.course_id == course.course_id
        ]
        modules.sort(key=lambda m: (m.position, m.module_id))
        module_ids = {module.module_id for module in modules}
        lessons = [
            lesson for lesson in self.lessons.values() if lesson.module_id in module_ids
        ]
        lessons.sort(key=lambda l: (l.position, l.lesson_id))
        lesson_ids = {lesson.lesson_id for lesson in lessons}
        assignments = [
            assignment
            for assignment in self.assignments.values()
            if assignment.lesson_id in lesson_ids
        ]
        assignments.sort(key=lambda a: a.assignment_id)
        return modules, lessons, assignments

    def publish_course(self, course_id: str, now: str) -> OwnedCourse:
        """The owned-data publication effect: one atomic business command.

        The whole hierarchy flips ``DRAFT → PUBLISHED`` or nothing does. The
        structural validation runs before the first write: every descendant
        must belong to the Course's Tenant and be in ``DRAFT``. Defence in
        depth: the registrations already refuse orphans and cross-tenant
        children, so a violation here means store data outside the published
        invariants — exactly what publication must refuse to certify.

        The validation and every write run inside the Course's domain
        critical section: no child creation can interleave between them, so
        the outcome is never a ``PUBLISHED`` Course with a ``DRAFT``
        descendant.
        """
        with self.hierarchy_section(course_id):
            course = self.courses[course_id]
            if course.status != "DRAFT":
                raise DomainRefusal("invalid_state_transition")
            modules, lessons, assignments = self._hierarchy_of_course(course)
            for module in modules:
                if module.tenant_id != course.tenant_id or module.status != "DRAFT":
                    raise DomainRefusal("invalid_state_transition")
            for lesson in lessons:
                if lesson.tenant_id != course.tenant_id or lesson.status != "DRAFT":
                    raise DomainRefusal("invalid_state_transition")
            for assignment in assignments:
                if (
                    assignment.tenant_id != course.tenant_id
                    or assignment.status != "DRAFT"
                ):
                    raise DomainRefusal("invalid_state_transition")
            # Validation passed — apply every transition, or none has happened:
            # nothing below can refuse, so no partial publication exists.
            published = replace(course, status="PUBLISHED", updated_at=now)
            self.courses[course_id] = published
            for module in modules:
                self.modules[module.module_id] = replace(
                    module, status="PUBLISHED", updated_at=now
                )
            for lesson in lessons:
                self.lessons[lesson.lesson_id] = replace(
                    lesson, status="PUBLISHED", updated_at=now
                )
            for assignment in assignments:
                self.assignments[assignment.assignment_id] = replace(
                    assignment, status="PUBLISHED", updated_at=now
                )
            return published

    def archive_course(self, course_id: str, now: str) -> OwnedCourse:
        """The owned-data archive effect: one atomic business command.

        Only a ``PUBLISHED`` Course can be archived, and the whole hierarchy
        flips ``PUBLISHED → ARCHIVED`` or nothing does. There is no
        unarchive: archived content is terminal in this slice.

        The validation and every write run inside the Course's domain
        critical section, like publication: no child creation and no
        publication can interleave between them.
        """
        with self.hierarchy_section(course_id):
            course = self.courses[course_id]
            if course.status != "PUBLISHED":
                raise DomainRefusal("invalid_state_transition")
            modules, lessons, assignments = self._hierarchy_of_course(course)
            for module in modules:
                if module.tenant_id != course.tenant_id or module.status != "PUBLISHED":
                    raise DomainRefusal("invalid_state_transition")
            for lesson in lessons:
                if lesson.tenant_id != course.tenant_id or lesson.status != "PUBLISHED":
                    raise DomainRefusal("invalid_state_transition")
            for assignment in assignments:
                if (
                    assignment.tenant_id != course.tenant_id
                    or assignment.status != "PUBLISHED"
                ):
                    raise DomainRefusal("invalid_state_transition")
            archived = replace(course, status="ARCHIVED", updated_at=now)
            self.courses[course_id] = archived
            for module in modules:
                self.modules[module.module_id] = replace(
                    module, status="ARCHIVED", updated_at=now
                )
            for lesson in lessons:
                self.lessons[lesson.lesson_id] = replace(
                    lesson, status="ARCHIVED", updated_at=now
                )
            for assignment in assignments:
                self.assignments[assignment.assignment_id] = replace(
                    assignment, status="ARCHIVED", updated_at=now
                )
            return archived

    def course_hierarchy(
        self, course_id: str
    ) -> tuple[
        OwnedCourse, list[OwnedModule], list[OwnedLesson], list[OwnedAssignment]
    ]:
        """The owned-data hierarchy-read operation. Called only after an ALLOW.

        Returns the Course and its complete descendant chain in deterministic
        order (position, then id): enough for a consumer to navigate
        ``Course → Module → Lesson → Assignment``. The read holds the
        Course's domain critical section, so it never observes a torn
        transition (e.g. a ``PUBLISHED`` Course with still-``DRAFT``
        descendants mid-publication).
        """
        with self.hierarchy_section(course_id):
            course = self.courses[course_id]
            modules, lessons, assignments = self._hierarchy_of_course(course)
            return course, modules, lessons, assignments

    # ------------------------------------------------------------ owned data
    def serve_submission(self, submission_id: str) -> OwnedSubmission:
        """The owned-data single-read operation. Called only after an ALLOW."""
        return self.submissions[submission_id]

    def list_submissions_for_assignment(
        self, assignment_id: str
    ) -> list[OwnedSubmission]:
        """The owned-data list operation. Called only after an ALLOW.

        Returns only submissions belonging to the requested Assignment, in
        deterministic ``submission_id`` order. No pagination, sorting or
        search parameters exist in this slice — the full set is the answer,
        and an empty set is a successful empty list.
        """
        items = [
            sub
            for sub in self.submissions.values()
            if sub.assignment_id == assignment_id
        ]
        items.sort(key=lambda s: s.submission_id)
        return items

    def assert_reviewable(self, submission_id: str) -> OwnedSubmission:
        """The state rule of the review command, enforced before idempotency.

        Only a ``SUBMITTED`` submission may be reviewed; review never changes
        the lifecycle state. Raises ``KeyError`` when the submission vanished
        after the decision and :class:`DomainRefusal` when its state is not
        ``SUBMITTED``.
        """
        submission = self.submissions[submission_id]
        if submission.status != "SUBMITTED":
            raise DomainRefusal("invalid_state_transition")
        return submission

    def apply_review(
        self, submission_id: str, reviewed_by: str, reviewed_at: str
    ) -> OwnedSubmission:
        """The owned-data review effect. Called only after an ALLOW and an IS-005
        decision to execute.

        Records the review fact — the verified Teacher identity and the
        timestamp — and leaves the submission ``SUBMITTED``. The state rule
        and the immutability of the review fact are re-checked here as well
        (defence in depth): the store never applies a review to a
        non-``SUBMITTED`` submission and never overwrites an existing review
        fact, whatever the calling command's binding is.
        """
        submission = self.submissions[submission_id]
        if submission.status != "SUBMITTED":
            raise DomainRefusal("invalid_state_transition")
        if submission.reviewed_by is not None or submission.reviewed_at is not None:
            # The review fact is a one-time immutable fact: a second review
            # command with a different Idempotency-Key is a new command that
            # must not overwrite the first review. (An exact replay of the
            # first command never reaches this method — IS-005 returns the
            # recorded result.)
            raise DomainRefusal("already_reviewed")
        updated = replace(submission, reviewed_by=reviewed_by, reviewed_at=reviewed_at)
        self.submissions[submission_id] = updated
        return updated

    # -------------------------------------------------------------- demo data
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant identifiers are the demo ones of IS-002: this component does
        not define Tenants, it only states which Tenant each record belongs
        to. Identity references are the demo ones of IS-001 (opaque ids —
        no profile data is stored here). The subjects/permissions are the
        demo ones of IS-003, granted by the composition root, so the full
        decision chain is exercisable against this store.
        """
        self.assignments = {}
        self.submissions = {}
        self.courses = {}
        self.modules = {}
        self.lessons = {}
        self.audit = []

        def add_course(
            course_id: str,
            tenant_id: str,
            title: str,
            description: str,
        ) -> None:
            self.register_course(
                OwnedCourse(
                    course_id=course_id,
                    tenant_id=tenant_id,
                    owner_component="learning",
                    title=title,
                    description=description,
                    status="DRAFT",
                    created_by="idn_human_a" if tenant_id == "ten_a" else "idn_human_b",
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                )
            )

        def add_module(
            module_id: str,
            course_id: str,
            tenant_id: str,
            title: str,
            position: int,
        ) -> None:
            self.register_module(
                OwnedModule(
                    module_id=module_id,
                    course_id=course_id,
                    tenant_id=tenant_id,
                    owner_component="learning",
                    title=title,
                    position=position,
                    status="DRAFT",
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                )
            )

        def add_lesson(
            lesson_id: str,
            module_id: str,
            tenant_id: str,
            title: str,
            content: str,
            position: int,
        ) -> None:
            self.register_lesson(
                OwnedLesson(
                    lesson_id=lesson_id,
                    module_id=module_id,
                    tenant_id=tenant_id,
                    owner_component="learning",
                    title=title,
                    content=content,
                    position=position,
                    status="DRAFT",
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                )
            )

        def add_assignment(
            assignment_id: str,
            tenant_id: str,
            status: str = "DRAFT",
            lesson_id: str | None = None,
            title: str | None = None,
            instructions: str | None = None,
        ) -> None:
            self.register_assignment(
                OwnedAssignment(
                    assignment_id=assignment_id,
                    tenant_id=tenant_id,
                    owner_component="learning",
                    status=status,
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                    lesson_id=lesson_id,
                    title=title,
                    instructions=instructions,
                )
            )

        def add_submission(
            submission_id: str,
            assignment_id: str,
            tenant_id: str,
            student_identity_id: str,
            attempt: int,
            status: str,
            content: dict[str, Any] | None = None,
        ) -> None:
            self.register_submission(
                OwnedSubmission(
                    submission_id=submission_id,
                    assignment_id=assignment_id,
                    tenant_id=tenant_id,
                    student_identity_id=student_identity_id,
                    attempt=attempt,
                    content=dict(content or {}),
                    status=status,
                    created_at="2026-09-08T00:00:00+00:00",
                    updated_at="2026-09-08T00:00:00+00:00",
                    owner_component="learning",
                )
            )

        # Tenant A: one published content hierarchy — Course → Module →
        # Lesson → Assignment (asg_a1, which also carries the demo
        # submissions of the downstream flow) — plus the two standalone
        # assignments of the submission slices.
        add_course(
            "crs_a1",
            "ten_a",
            "Foundations of Learning",
            "The demo published course of Tenant A.",
        )
        add_module("mod_a1", "crs_a1", "ten_a", "Getting Started", 1)
        add_lesson(
            "les_a1",
            "mod_a1",
            "ten_a",
            "Welcome",
            "Read this lesson before submitting any work.",
            1,
        )
        add_assignment(
            "asg_a1",
            "ten_a",
            lesson_id="les_a1",
            title="First steps",
            instructions="Submit the first exercise.",
        )
        add_assignment("asg_a2", "ten_a", status="PUBLISHED")
        add_assignment("asg_a_empty", "ten_a", status="PUBLISHED")
        # The demo hierarchy becomes published through the real publication
        # effect — the same code path the command uses; there is no second way
        # to bring a hierarchy to PUBLISHED.
        self.publish_course("crs_a1", "2026-09-08T00:00:00+00:00")
        # Tenant B: one published content hierarchy with one submission
        # (isolation proof).
        add_course(
            "crs_b1",
            "ten_b",
            "Foundations of Learning B",
            "The demo published course of Tenant B.",
        )
        add_module("mod_b1", "crs_b1", "ten_b", "Getting Started B", 1)
        add_lesson(
            "les_b1",
            "mod_b1",
            "ten_b",
            "Welcome B",
            "Read this lesson before submitting any work.",
            1,
        )
        add_assignment(
            "asg_b1",
            "ten_b",
            lesson_id="les_b1",
            title="First steps B",
            instructions="Submit the first exercise.",
        )
        self.publish_course("crs_b1", "2026-09-08T00:00:00+00:00")

        add_submission("sub_a1_1", "asg_a1", "ten_a", "idn_human_c", 1, "SUBMITTED")
        add_submission("sub_a1_2", "asg_a1", "ten_a", "idn_human_c", 2, "DRAFT")
        add_submission("sub_a2_1", "asg_a2", "ten_a", "idn_human_c", 1, "SUBMITTED")
        add_submission("sub_b1_1", "asg_b1", "ten_b", "idn_human_b", 1, "SUBMITTED")
