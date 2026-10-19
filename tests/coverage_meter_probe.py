"""Run by tests/test_test_sessions.py in a clean process: exercise the real CoverageMeter.

A request thread that already exists when the session starts must still be measured (that is
why the meter uses sys.monitoring), and the meter must step aside under an outer coverage run.
Prints one JSON object.
"""

import json
import queue
import threading
from dataclasses import asdict
from datetime import UTC, datetime

import coverage

from app.modules.testing.code_coverage import CoverageMeter


def main() -> None:
    jobs: queue.Queue = queue.Queue()
    done: queue.Queue = queue.Queue()

    def worker() -> None:  # stands in for a server thread started before the session
        while (job := jobs.get()) is not None:
            done.put(job())

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    meter = CoverageMeter()
    meter.restart()
    before = asdict(meter.snapshot())

    def project_request() -> str:
        from app.modules.events.models import retain_until_for

        return retain_until_for(datetime(2026, 8, 31, tzinfo=UTC)).isoformat()

    jobs.put(project_request)
    done.get(timeout=30)
    after = asdict(meter.snapshot())
    again = asdict(meter.snapshot())

    # Stop from another thread than the one that started it, as the server may.
    jobs.put(meter.stop)
    done.get(timeout=30)
    jobs.put(None)
    stopped = asdict(meter.snapshot())

    outer = coverage.Coverage(data_file=None, config_file=False)
    outer.start()
    try:
        nested = CoverageMeter()
        nested.restart()
        outer_result = asdict(nested.snapshot())
    finally:
        outer.stop()

    print(
        json.dumps(
            {
                "before": before,
                "after": after,
                "again": again,
                "stopped": stopped,
                "outer": outer_result,
            }
        )
    )


if __name__ == "__main__":
    main()
