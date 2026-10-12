"""Control-flow exceptions of the launch-session runner.

Each one tells the runner what to do with the session when a stage stops:

  OwnershipLost   another worker holds the session now (its claim bumped
                  ``claims``): write nothing more, exit quietly.
  LeaseExpired    our heartbeats have failed long enough that another worker
                  may claim it: start nothing new, try an owner-scoped release.
  Shutdown        this process is stopping (core.tasks.stopping): release the
                  lease so another worker adopts the session; never a failure.
  CancelRequested the recruiter cancelled a running session: finalize_cancel.
  OutreachStopped /candidates/save or Pairbot answered 409 (job activity
                  stopped): terminal ``cancelled`` ('outreach stopped').
  AccessDenied    a 403 for the requester: retried, then ``failed``.
  RetryLater      not an error: the stage must wait (Pairbot still creating a
                  bulk) — release with not_before, attempt NOT counted.
"""

from __future__ import annotations


class LaunchSessionStop(Exception):
    """Base of the runner's control-flow exceptions."""


class OwnershipLost(LaunchSessionStop):
    pass


class LeaseExpired(LaunchSessionStop):
    pass


class Shutdown(LaunchSessionStop):
    pass


class CancelRequested(LaunchSessionStop):
    pass


class OutreachStopped(LaunchSessionStop):
    pass


class AccessDenied(LaunchSessionStop):
    pass


class RetryLater(LaunchSessionStop):
    def __init__(self, delay_s: float, note: str = "") -> None:
        super().__init__(note or f"retry in {delay_s:.0f}s")
        self.delay_s = max(1.0, float(delay_s))
        self.note = note
