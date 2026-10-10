import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.processes import Control, worker_progress_scope


class WorkerProgressTests(unittest.TestCase):
    def test_progress_is_delivered_before_worker_exit(self):
        events = []
        program = ("import json,time; print(json.dumps({'worker_progress':"
            "{'fraction':.25,'processed':1,'total':4,'message':'chunk'}}),flush=True);"
            "time.sleep(.4); print('done',flush=True)")
        with worker_progress_scope(lambda event: events.append((time.monotonic(), event))):
            result = Control().run([sys.executable, "-B", "-c", program], timeout=10)
        finished = time.monotonic()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(events), 1)
        self.assertGreater(finished - events[0][0], .25)
        self.assertEqual(events[0][1]["processed"], 1)
        self.assertIn(b"worker_progress", result.stdout)
        self.assertIn(b"done", result.stdout)

    def test_invalid_fraction_does_not_become_progress(self):
        events = []
        program = ("import json; print(json.dumps({'worker_progress':{'fraction':2}}),flush=True);"
                   "print(json.dumps({'worker_progress':{'fraction':float('nan')}}),flush=True)")
        with worker_progress_scope(events.append):
            result = Control().run([sys.executable, "-B", "-c", program], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
