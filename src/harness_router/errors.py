class RouterError(Exception):
    """Base exception for harness-router."""


class ProviderError(RouterError):
    """The decision provider failed."""


class ProviderTimeoutError(ProviderError):
    """The decision provider timed out."""


class InvalidProviderResponse(RouterError):
    """The decision provider returned an invalid response."""


class UnknownToolError(RouterError):
    """The decision provider selected a tool that is not registered."""


class PolicyDeniedError(RouterError):
    """Execution was rejected by policy."""


class RoutingLoopError(RouterError):
    """A routing loop was detected."""


class RouterConfigurationError(RouterError):
    """Router configuration is invalid or incomplete."""
