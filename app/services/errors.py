"""Exceptions raised by the service layer.

Services never import anything from fastapi or starlette - they raise one of
these instead, and the thin route functions in app/routes/ are the only place
that knows how to turn a given exception into an HTTP status code and a
message. This is what makes each service independently testable (a test can
call create_expense() directly and assert it raises ValidationError, with no
web server involved) and independently upgradable (swap the storage engine,
change a validation rule, etc. without touching any route file).
"""


class ServiceError(Exception):
    """Base class for all service-layer errors."""


class ValidationError(ServiceError):
    """The caller-supplied data is invalid (bad amount, unknown category, ...)."""


class NotFoundError(ServiceError):
    """The requested record doesn't exist."""


class ConflictError(ServiceError):
    """The record exists but isn't in a state the requested action allows
    (e.g. approving a voucher that's already been approved by someone else)."""


class DuplicateError(ServiceError):
    """A uniqueness constraint the caller should have checked was violated
    (e.g. a username that's already taken)."""


class ForbiddenError(ServiceError):
    """The caller's *role* is allowed to attempt this action in general
    (already checked by app/routes/guards.py before the service was ever
    called), but this specific record belongs to someone else and the
    caller isn't allowed to act on it anyway - e.g. editing another user's
    draft voucher. Role checks and record-ownership checks are different
    concerns, so they get different exceptions."""
