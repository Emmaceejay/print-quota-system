"""Exception hierarchy shared across printquota components."""


class PrintQuotaError(Exception):
    """Base class for all printquota errors."""


class ConfigError(PrintQuotaError):
    """Configuration is missing or malformed."""


class UnknownUserError(PrintQuotaError):
    """The job-originating user has no record in the datastore."""


class PolicyViolation(PrintQuotaError):
    """A job was rejected by the policy engine.

    Attributes:
        reason: Human-readable reason, written to the job record and logs.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class QuotaExceeded(PolicyViolation):
    """A job was rejected because it does not fit the remaining quota."""


class EstimationError(PrintQuotaError):
    """Page count could not be estimated for a job payload."""
