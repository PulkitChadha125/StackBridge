"""Licence: StakBridge signs in to the licence server (the SuperAdmin panel, superadmin_panel/).

On when a licence server address is known: baked into the EXE by the panel's "Build EXE" button,
or set with STAKBRIDGE_LICENSE_SERVER. Without one (running from source for development) the web
page keeps its own local password and nothing here is used.

Only outbound HTTPS calls to the licence server: sign in, licence check every 30 minutes, client
limit, telemetry every 15 minutes, and the SuperAdmin's log requests. Broker passwords, trades and
account data never leave this PC; the server only learns counts.
"""

from .service import LicenseError, Licensing

__all__ = ["LicenseError", "Licensing"]
