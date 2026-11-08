"""Only explicit capacity errors permit moving to another launch candidate."""

from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from google.api_core.exceptions import NotFound


class CapacityUnavailableError(Exception):
    pass


class InstanceIdentityError(Exception):
    pass


class CreateRejectedError(Exception):
    """The provider explicitly rejected insert before accepting an operation."""


def is_create_rejected(exc: Exception) -> bool:
    if isinstance(
        exc,
        CreateRejectedError | ValueError | NoCredentialsError | PartialCredentialsError,
    ):
        return True
    return isinstance(exc, ClientError) and exc.response["Error"]["Code"] in {
        "InvalidAMIID.NotFound",
        "InvalidAMIID.Malformed",
        "InvalidSubnetID.NotFound",
        "InvalidGroup.NotFound",
        "InvalidParameterValue",
        "InvalidParameterCombination",
        "MissingParameter",
        "Unsupported",
        "UnsupportedOperation",
        "UnauthorizedOperation",
        "AuthFailure",
        "OptInRequired",
        "InvalidKeyPair.NotFound",
        "ExpiredToken",
        "InvalidClientTokenId",
    }


def is_capacity_error(exc: Exception) -> bool:
    if isinstance(exc, CapacityUnavailableError):
        return True
    return isinstance(exc, ClientError) and exc.response["Error"]["Code"] in {
        "InsufficientInstanceCapacity",
        "InsufficientHostCapacity",
        "InsufficientFreeAddressesInSubnet",
        "MaxSpotInstanceCountExceeded",
        "VcpuLimitExceeded",
    }


def is_not_found(exc: Exception) -> bool:
    return isinstance(exc, NotFound) or (
        isinstance(exc, ClientError)
        and exc.response["Error"]["Code"] == "InvalidInstanceID.NotFound"
    )
