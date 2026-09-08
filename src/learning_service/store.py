"""Owned data of the Learning component (logical schema ``learning``).

This module is internal. No other component has direct access to it: courses,
modules, lessons, assignments, enrollments, submissions and the audit journal
are reachable only through the published contract of this component
(ARCHITECTURE.md §1.1, LAW-04).

The store separates the two ways its data is touched, and the separation is
the enforcement story of the component:

* lookups (:meth:`LearningStore.course`, :meth:`LearningStore.assignment`,
  …) — enforcement metadata: whether an entity exists here and which Tenant
  owns it. The engine reads them to form the questions it asks IS-001 and
  IS-003; they serve nothing to a caller;
* the owned-data operations (:meth:`LearningStore.save_course`,
  :meth:`LearningStore.apply_publish`, …) — the operations that create or
  change business state. The engine calls them only after the enforcement
  chain produced an ALLOW, and the tests count exactly these calls to prove
  that a denied request never reaches them and a replay never repeats them.

A domain rule that only the owner knows (the course state machine, the
duplicate-enrollment uniqueness) is enforced here as well as in the engine:
the final state of owned data is decided by its owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from learning_service.contracts import ErrorCode
from learning_service.models import (
    AccessAuditEvent,
    Assignment,
    Course,
    COURSE_TRANSITIONS,
    Enrollment,
    Lesson,
    Module,
    Submission,
)


class DomainRefusal(Exception):
    """A domain-level refusal of an otherwise allowed operation.

    Raised only inside the owned-data step of the chain; audited like every
    refusal. ``code`` is one of the published error codes.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class LearningStore:
    """In-memory owned storage of the Learning business data and the audit journal."""

    courses: dict[str, Course] = field(default_factory=dict)
    modules: dict[str, Module] = field(default_factory=dict)
    lessons: dict[str, Lesson] = field(default_factory=dict)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    enrollments: dict[str, Enrollment] = field(default_factory=dict)
    submissions: dict[str, Submission] = field(default_factory=dict)
    audit: list[AccessAuditEvent] = field(default_factory=list)

    # -------------------------------------------------------------- lookups
    def course(self, course_id: str) -> Course | None:
        return self.courses.get(course_id)

    def module(self, module_id: str) -> Module | None:
        return self.modules.get(module_id)

    def lesson(self, lesson_id: str) -> Lesson | None:
        return self.lessons.get(lesson_id)

    def assignment(self, assignment_id: str) -> Assignment | None:
        return self.assignments.get(assignment_id)

    def submission(self, submission_id: str) -> Submission | None:
        return self.submissions.get(submission_id)

    def enrollment_of(self, course_id: str, student_identity_id: str) -> Enrollment | None:
        """The enrollment of one student in one course, if it exists."""
        for enrollment in self.enrollments.values():
            if (
                enrollment.course_id == course_id
                and enrollment.student_identity_id == student_identity_id
            ):
                return enrollment
        return None

    def modules_of_course(self, course_id: str) -> list[Module]:
        return [m for m in self.modules.values() if m.course_id == course_id]

    def lessons_of_module(self, module_id: str) -> list[Lesson]:
        return [l for l in self.lessons.values() if l.module_id == module_id]

    def assignments_of_lesson(self, lesson_id: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.lesson_id == lesson_id]

    # -------------------------------------------------- owned-data operations
    def save_course(self, course: Course) -> Course:
        """Create one course; the id is server-generated and never re-owned."""
        if course.course_id in self.courses:
            raise ValueError(f"course {course.course_id!r} already exists")
        self.courses[course.course_id] = course
        return course

    def apply_publish(self, course_id: str, timestamp: str) -> tuple[Course, int]:
        """The Publish Course command on owned data, including its consequences.

        The only allowed transition is ``DRAFT → PUBLISHED``. Publishing a
        course publishes its content tree with it: modules, lessons and
        assignments of the course become ``PUBLISHED`` in the same command —
        no other command in this slice can change their state. The returned
        count is the number of content entities the cascade published.

        Raises :class:`DomainRefusal` (``INVALID_STATE_TRANSITION``) when the
        course is not in ``DRAFT``.
        """
        course = self.courses[course_id]
        expected_from, target = COURSE_TRANSITIONS["publish"]
        if course.status != expected_from:
            raise DomainRefusal(ErrorCode.INVALID_STATE_TRANSITION.value)
        published = replace(course, status=target, updated_at=timestamp)
        self.courses[course_id] = published

        cascade = 0
        for module in self.modules_of_course(course_id):
            self.modules[module.module_id] = replace(
                module, status="PUBLISHED", updated_at=timestamp
            )
            cascade += 1
            for lesson in self.lessons_of_module(module.module_id):
                self.lessons[lesson.lesson_id] = replace(
                    lesson, status="PUBLISHED", updated_at=timestamp
                )
                cascade += 1
                for assignment in self.assignments_of_lesson(lesson.lesson_id):
                    self.assignments[assignment.assignment_id] = replace(
                        assignment, status="PUBLISHED"
                    )
                    cascade += 1
        return published, cascade

    def save_module(self, module: Module) -> Module:
        if module.module_id in self.modules:
            raise ValueError(f"module {module.module_id!r} already exists")
        self.modules[module.module_id] = module
        return module

    def save_lesson(self, lesson: Lesson) -> Lesson:
        if lesson.lesson_id in self.lessons:
            raise ValueError(f"lesson {lesson.lesson_id!r} already exists")
        self.lessons[lesson.lesson_id] = lesson
        return lesson

    def save_assignment(self, assignment: Assignment) -> Assignment:
        if assignment.assignment_id in self.assignments:
            raise ValueError(f"assignment {assignment.assignment_id!r} already exists")
        self.assignments[assignment.assignment_id] = assignment
        return assignment

    def save_enrollment(self, enrollment: Enrollment) -> Enrollment:
        """Create one enrollment; one student is enrolled in a course once."""
        if self.enrollment_of(enrollment.course_id, enrollment.student_identity_id) is not None:
            raise DomainRefusal(ErrorCode.DUPLICATE_ENROLLMENT.value)
        self.enrollments[enrollment.enrollment_id] = enrollment
        return enrollment

    def save_submission(self, submission: Submission) -> Submission:
        if submission.submission_id in self.submissions:
            raise ValueError(f"submission {submission.submission_id!r} already exists")
        self.submissions[submission.submission_id] = submission
        return submission

    # ---------------------------------------------------------------- audit
    def append_audit(self, event: AccessAuditEvent) -> AccessAuditEvent:
        """Append one audit record; the journal is append-only."""
        self.audit.append(event)
        return event

    # ------------------------------------------------------------- demo data
    def seed_demo(self) -> None:
        """Fixed demonstration data for the standalone Level 0 deployment.

        Tenant and identity identifiers are the demo ones of IS-001/IS-002:
        this component does not define them, it only states which Tenant each
        of its courses belongs to. Two draft courses (one per demo Tenant)
        are enough to explore the surface; everything else is created through
        the published commands, which is the only path there is.
        """
        self.courses = {}
        self.modules = {}
        self.lessons = {}
        self.assignments = {}
        self.enrollments = {}
        self.submissions = {}
        self.audit = []

        stamp = "2026-09-08T00:00:00+00:00"
        self.save_course(
            Course(
                course_id="crs_demo_a",
                tenant_id="ten_a",
                title="Demo Course A",
                description="Draft demo course of Tenant A.",
                status="DRAFT",
                created_by="idn_human_a",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.save_course(
            Course(
                course_id="crs_demo_b",
                tenant_id="ten_b",
                title="Demo Course B",
                description="Draft demo course of Tenant B.",
                status="DRAFT",
                created_by="idn_human_b",
                created_at=stamp,
                updated_at=stamp,
            )
        )
