"""Eval harness (DESIGN.md §10 datasets/experiments, §12 M3).

collect.py  production traces scored user_edited=1 → Langfuse dataset 'usual-corrections'
replay.py   a dataset → the SAME decompose + resolver cascade as production, recorded as a
            Langfuse dataset run (experiment) under environment=test

See README.md in this directory for the three-command loop.
"""
