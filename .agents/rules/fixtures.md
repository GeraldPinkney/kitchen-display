---
trigger: glob
globs: "tests/fixtures/*, tests/fixtures/*.json"
description: "Protect offline HA state fixtures from edits during feature work."
---

# HA state fixtures are read-only contracts

Never edit, regenerate, or delete files under `tests/fixtures/` unless the prompt explicitly instructs a fixture re-capture. Fix `ha/kitchen_dashboard.yaml` or application code instead.
