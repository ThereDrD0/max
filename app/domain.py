from __future__ import annotations


class BotDomainError(Exception):
    """Base class for expected business errors."""


class ConsentRequiredError(BotDomainError):
    pass


class AccessDeniedError(BotDomainError):
    pass


class EventNotFoundError(BotDomainError):
    pass


class RegistrationNotFoundError(BotDomainError):
    pass


class RegistrationClosedError(BotDomainError):
    pass


class SlotRequiredError(BotDomainError):
    pass


class SlotNotFoundError(BotDomainError):
    pass


class NoSeatsAvailableError(BotDomainError):
    pass


class DuplicateActiveRegistrationError(BotDomainError):
    pass


class DuplicateEventSlugError(BotDomainError):
    pass


class LateCancellationDeniedError(BotDomainError):
    pass


class InvalidNotificationKindError(BotDomainError):
    pass
