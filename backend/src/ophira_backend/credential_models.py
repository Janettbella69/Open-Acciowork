"""Human credential management contracts shared with offline archives."""

from enum import StrEnum

from pydantic import Field, SecretStr, StrictBool, model_validator

from .models import APIModel


class CredentialKind(StrEnum):
    PROVIDER = "provider"
    CONNECTOR = "connector"
    SERVICE = "service"
    DEPLOYMENT = "deployment"


class CredentialReview(APIModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class CredentialExport(CredentialReview):
    password: SecretStr = Field(min_length=12, max_length=1024)


class CredentialSave(CredentialReview):
    secret: SecretStr = Field(min_length=1, max_length=2000)


class CredentialDelete(CredentialReview):
    confirm_delete: StrictBool

    @model_validator(mode="after")
    def confirmed(self):
        if not self.confirm_delete:
            raise ValueError("Explicit deletion confirmation is required")
        return self
