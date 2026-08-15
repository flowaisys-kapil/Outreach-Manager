from enum import Enum


class ProfileState(str, Enum):
    QUALIFIED = "Qualified"
    PENDING = "Pending"
    CONNECTED = "Connected"
    NOT_CONNECTED = "Not Connected"
    WITHDRAWN = "Withdrawn"
    FAILED = "Failed"
    COMPLETED = "Completed"
