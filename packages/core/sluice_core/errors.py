class SluiceError(Exception): ...


class KeyNotFound(SluiceError): ...


class UnknownAckToken(SluiceError): ...


class ProvisionFailure(SluiceError):
    def __init__(self, kind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
