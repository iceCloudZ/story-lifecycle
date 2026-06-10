"""Custom exceptions for contact verification."""


class ContactVerificationError(Exception):
    """Base error for contact verification failures."""

    def __init__(self, message: str, provider: str = ""):
        self.message = message
        self.provider = provider
        super().__init__(message)


class ContactValidationError(Exception):
    """Error for invalid contact input during verification."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)
