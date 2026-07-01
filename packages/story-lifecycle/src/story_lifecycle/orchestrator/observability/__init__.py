"""Observability sub-package — debug-packet building, diagnostics bundles,
and debug-event logging/response.

Stage-1 layer partition (ISS-010): ``debug_packet`` / ``diagnostics`` moved
here unchanged, and the root ``observability.py`` moved + renamed to
``observability/events.py`` (it logs/loads debug events and builds the debug
response) to avoid an ``observability.observability`` path collision.
"""
