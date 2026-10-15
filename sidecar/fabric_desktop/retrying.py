"""Which errors are worth another try: the network, throttling, a busy table, a lost commit race.

Everything else (a check that failed, bad config, a missing table, no permission) fails the same
way the next time, so the agent reports it instead of retrying it.
"""

from __future__ import annotations

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def is_retryable(exc: BaseException) -> bool:
    import requests
    from deltalake.exceptions import CommitFailedError

    from .leases import LeaseBusy

    if isinstance(exc, (LeaseBusy, CommitFailedError, requests.ConnectionError, requests.Timeout,
                        ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in TRANSIENT_STATUS
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return isinstance(code, int) and code in TRANSIENT_STATUS
