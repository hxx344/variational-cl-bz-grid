"""Long monitor reads must not stop ordinary sampling at the reset gate."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from variational_grid.models import GridError
from variational_grid.reset import control_lock, initialize, process_reset, read_state, request_reset


class ResetIdleTests(unittest.TestCase):
    def test_idle_writer_bypasses_read_lock_but_pending_reset_remains_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = SimpleNamespace(output=Path(directory))
            cohort = SimpleNamespace(experiment=experiment)
            initialize(experiment)
            with control_lock(experiment):
                self.assertFalse(process_reset(cohort))
            generation = read_state(experiment)["generation"]
            request_reset(experiment, generation)
            with control_lock(experiment):
                with self.assertRaises(GridError):
                    process_reset(cohort)
            self.assertEqual(read_state(experiment)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
