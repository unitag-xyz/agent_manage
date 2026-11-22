class APIError(Exception):
    """A safe, transport-independent error. Never carries native credentials."""

    def __init__(self, code, message, *, details=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.details = details
        self.retryable = retryable

    def payload(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable,
                **({"details": self.details} if self.details is not None else {})}
