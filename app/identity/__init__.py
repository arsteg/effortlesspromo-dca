"""DCA-014 — the Python identity/membership contract (the ``auth-port`` seam, blocker B1).

The DCA never owns users or credentials — identity and membership live in the platform (.NET API).
This package defines *only the contract* the DCA uses to ask the platform "may subject S act on
business B, with which roles, until when?": typed request/response models, a client ``Protocol``, and
a deterministic in-memory fake for local tests. The live HTTP adapter is a LATER story and stays
unbuilt; per the DCA boundary a missing contract stays disabled, and production refuses the fake.
"""

from __future__ import annotations

from app.identity.contract import (
    IdentityClient,
    IdentityRequest,
    IdentityResponse,
    Membership,
    MembershipStatus,
)
from app.identity.fake import (
    FakeIdentityClient,
    IdentityContractDisabled,
    build_identity_client,
)
from app.identity.service_credentials import (
    JwtServiceCredentialVerifier,
    ServiceCredential,
    ServiceCredentialError,
    ServiceCredentialVerifier,
    build_service_credential_verifier,
)
from app.identity.tenant_context import (
    TenantContext,
    TenantResolutionError,
    resolve_tenant_context,
)
from app.identity.tokens import (
    AuthenticatedPrincipal,
    FakeTokenVerifier,
    JwtTokenVerifier,
    TokenValidationError,
    TokenVerifier,
    build_token_verifier,
)

__all__ = [
    "AuthenticatedPrincipal",
    "FakeIdentityClient",
    "FakeTokenVerifier",
    "IdentityClient",
    "IdentityContractDisabled",
    "IdentityRequest",
    "IdentityResponse",
    "JwtServiceCredentialVerifier",
    "JwtTokenVerifier",
    "Membership",
    "MembershipStatus",
    "ServiceCredential",
    "ServiceCredentialError",
    "ServiceCredentialVerifier",
    "TenantContext",
    "TenantResolutionError",
    "TokenValidationError",
    "TokenVerifier",
    "build_identity_client",
    "build_service_credential_verifier",
    "build_token_verifier",
    "resolve_tenant_context",
]
