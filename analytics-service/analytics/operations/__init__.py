"""Stage 10: operational reliability, recovery and lifecycle management.

Not a pipeline stage. The parts that let the other nine run unattended:
bounded recovery, explicit replay, retention, and a health view that says why.

The distinction everything here is built on:

    RECOVERY      moves a stuck record into an honest, final-or-actionable
                  state. It never repeats work.
    RE-EXECUTION  repeats work. It happens only where repeating is provably
                  safe, and never as a side effect of recovery.
"""
