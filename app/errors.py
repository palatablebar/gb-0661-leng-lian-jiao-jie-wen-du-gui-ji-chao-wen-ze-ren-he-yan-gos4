class RuleError(ValueError):
    def __init__(self, code, message, status=422, extra=None):
        self.code, self.message, self.status = code, message, status
        self.extra = extra or {}
        super().__init__(message)
