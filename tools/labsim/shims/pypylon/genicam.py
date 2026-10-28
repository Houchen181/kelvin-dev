"""GenICam exceptions, as pypylon.genicam has them."""


class GenericException(Exception):
    def GetDescription(self) -> str:
        return str(self)


class RuntimeException(GenericException):
    pass


class LogicalErrorException(GenericException):
    pass


class TimeoutException(GenericException):
    pass


class OutOfRangeException(GenericException):
    pass


class InvalidArgumentException(GenericException):
    pass


class AccessException(GenericException):
    pass


class BadAllocException(GenericException):
    pass


def _raise(kind, msg):
    raise {"RuntimeException": RuntimeException, "LogicalErrorException": LogicalErrorException,
           "TimeoutException": TimeoutException, "OutOfRangeException": OutOfRangeException,
           "InvalidArgumentException": InvalidArgumentException, "AccessException": AccessException
           }.get(kind or "", RuntimeException)(msg)
