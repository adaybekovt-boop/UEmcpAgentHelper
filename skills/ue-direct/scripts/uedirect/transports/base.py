from ..util import UedHostError


class TransportError(UedHostError):
    def __init__(self, message, code="TRANSPORT_UNAVAILABLE", hint=None, retryable=True):
        UedHostError.__init__(self, code, message, hint)
        self.retryable = retryable


class Transport(object):
    name = "base"

    def probe(self, timeout=2.0):
        """Return identity info (dict) if an editor answers, else None. Must be cheap."""
        raise NotImplementedError

    def execute(self, code, timeout=110.0):
        """Run Python ``code`` in the editor. Return text output. Raise TransportError."""
        raise NotImplementedError

    def describe(self):
        return {"transport": self.name}
