"""Learning sub-package — quality-flywheel seeding (seed_pipeline + seeds)
and reflection (playbook persistence + transition history facts).

Seeding lives in its own subpackage to keep the dependency direction strict:
learning -> engine -> evaluation (one-way, no cycle).
"""
