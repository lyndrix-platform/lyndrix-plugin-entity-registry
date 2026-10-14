"""Test bootstrap.

Importing ``core.api`` cold trips a circular import inside lyndrix-core
(core.api → plugins → ui.layout → notifications → core.api). The running
platform never hits it because main.py loads core.services first; mirror that
order here before any plugin module pulls in core.api.
"""
# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401
