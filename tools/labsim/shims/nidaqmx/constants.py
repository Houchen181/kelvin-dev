import enum


class TerminalConfiguration(enum.Enum):
    DEFAULT = -1
    RSE = 10083
    NRSE = 10078
    DIFF = 10106
    PSEUDO_DIFF = 12529


class AcquisitionType(enum.Enum):
    FINITE = 10178
    CONTINUOUS = 10123
    HW_TIMED_SINGLE_POINT = 12522


class Edge(enum.Enum):
    RISING = 10280
    FALLING = 10171


class LineGrouping(enum.Enum):
    CHAN_PER_LINE = 0
    CHAN_FOR_ALL_LINES = 1


class CountDirection(enum.Enum):
    COUNT_UP = 10128
    COUNT_DOWN = 10124


READ_ALL_AVAILABLE = -1
