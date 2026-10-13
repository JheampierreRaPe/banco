"""Esquemas Pydantic de credits (E3-T03 simulador, E3-T08 expediente,
E3-T12 juego contractual, E3-T14 firma)."""

from app.modules.credits.schemas.applications import (
    ApplicationCreateRequest,
    ApplicationData,
    ApplicationResponse,
    ApplicationsListResponse,
    OwnApplicationData,
    ReviewRequest,
)
from app.modules.credits.schemas.contracts import (
    ContractDocument,
    ContractSetData,
    ContractSetResponse,
)
from app.modules.credits.schemas.signing import (
    ChallengeData,
    ChallengeRequest,
    ChallengeResponse,
    SignContractData,
    SignContractRequest,
    SignContractResponse,
    SignedDocumentOut,
)
from app.modules.credits.schemas.simulation import (
    ProductOut,
    ProductsListResponse,
    ScheduleRowOut,
    SimulateData,
    SimulateRequest,
    SimulateResponse,
)

__all__ = [
    "ApplicationCreateRequest",
    "ApplicationData",
    "ApplicationResponse",
    "ApplicationsListResponse",
    "ChallengeData",
    "ChallengeRequest",
    "ChallengeResponse",
    "ContractDocument",
    "ContractSetData",
    "ContractSetResponse",
    "OwnApplicationData",
    "ProductOut",
    "ProductsListResponse",
    "ReviewRequest",
    "ScheduleRowOut",
    "SignContractData",
    "SignContractRequest",
    "SignContractResponse",
    "SignedDocumentOut",
    "SimulateData",
    "SimulateRequest",
    "SimulateResponse",
]
