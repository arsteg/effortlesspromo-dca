"""Activation references — short-lived, tamper-resistant proofs that a contact point cleared the
DCA's compliance gates for one channel at one moment (DCA-094).

A reference is NOT send permission: it binds a single, fully-qualified activation context into a
signed, expiring token so a later consumer can prove the eligibility it relies on was issued by the
DCA, for exactly this tenant/recipient/channel/purpose/provider/campaign-revision/permission+source
versions, and has not been altered or expired. The one send pipeline still owns and re-gates the send.
"""
