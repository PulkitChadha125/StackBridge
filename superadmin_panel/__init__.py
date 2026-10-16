"""SuperAdmin Control Plane - separate app, own DB, own auth.

Deliberately not merged into the StakBridge app (stakbridge/) -
different auth model, no MT5 dependency, and it must
keep working even if an individual Admin instance is broken/offline.
"""
