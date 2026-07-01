"""Evaluation sub-package — verify gate, repair-packet building, and the
quality flywheel (findings / learned-patterns).

Stage-1 layer partition (ISS-010): ``gate`` / ``evaluator_loop`` / ``quality``
/ ``review_feedback`` moved here from the orchestrator root so the quality +
gate concerns are physically grouped (high cohesion) and can be reasoned about
and tested as one cluster.
"""
