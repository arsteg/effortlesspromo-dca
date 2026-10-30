"""DCA-095 — revalidate immediately before host dispatch.

The consume/recheck seam that sits between a DCA-094 activation reference and a host's actual send:
it re-asserts the DCA's OWN gates (permission, suppression and version state) at dispatch time and
consumes exactly one dispatch slot idempotently. See :mod:`app.dispatch.consumption`.
"""
