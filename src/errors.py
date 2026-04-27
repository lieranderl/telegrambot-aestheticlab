class StateStoreConflictError(RuntimeError):
    pass


class StateStoreUnavailableError(RuntimeError):
    pass


class WebhookAuthenticationError(RuntimeError):
    pass


class WebhookProcessingError(RuntimeError):
    pass
