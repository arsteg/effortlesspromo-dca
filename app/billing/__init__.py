"""DCA-158 — the usage-billing contract: deliver usage ONLY through EffortlessPromo platform billing.

The DCA owns no metering, no price book and no charge path; the EffortlessPromo platform owns billing. This
package defines only the contract the DCA uses to DELIVER one DCA-157 billable-acceptance usage fact to the
platform under a stable request key: typed request models (a stable key + a usage record with an opaque,
non-charge provider cost basis), the acknowledgment receipt (a host handle + the platform-minted customer
price), the client ``Protocol`` (one method, ``deliver`` — no charge/invoice/price method), the fail-closed
rule that DISABLES a delivery whose meter has no configured price (inventing no charge), a builder from a
billable acceptance, and a deterministic synthetic fake for local tests. A delivery is never the DCA
charging anything; PENDING and ACKNOWLEDGED outcomes differ (only an ack is a durable meter). The live
adapter is a later story (DCA-001 B1) and stays unbuilt; production refuses the fake.
"""

from __future__ import annotations

from app.billing.contract import (
    DEFAULT_CURRENCY,
    DEFAULT_MAX_CAPABILITIES_AGE,
    BillingCapabilities,
    BillingHostAvailability,
    UsageBillingPort,
    UsageDeliveryDecision,
    UsageDeliveryRequest,
    UsageDeliveryStatus,
    UsageMeter,
    UsageReceipt,
    UsageRecord,
    authorize_usage_delivery,
    usage_record_from_entry,
)
from app.billing.fake import (
    DEFAULT_CUSTOMER_PRICE_MINOR,
    DEFAULT_PRICED_METERS,
    SYNTHETIC_VERSION,
    BillingUsageContractDisabled,
    FakeEffortlessPromoBillingPort,
    build_billing_usage_port,
)

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_CUSTOMER_PRICE_MINOR",
    "DEFAULT_MAX_CAPABILITIES_AGE",
    "DEFAULT_PRICED_METERS",
    "SYNTHETIC_VERSION",
    "BillingCapabilities",
    "BillingHostAvailability",
    "BillingUsageContractDisabled",
    "FakeEffortlessPromoBillingPort",
    "UsageBillingPort",
    "UsageDeliveryDecision",
    "UsageDeliveryRequest",
    "UsageDeliveryStatus",
    "UsageMeter",
    "UsageReceipt",
    "UsageRecord",
    "authorize_usage_delivery",
    "build_billing_usage_port",
    "usage_record_from_entry",
]
