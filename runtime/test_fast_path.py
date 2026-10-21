"""CPU-only tests for refusing fallback bindings and auditing executed kernels."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import fast_path


def kernel(*args, **kwargs):
    return "result"


class DeltaLayer:
    causal_conv1d_fn = staticmethod(kernel)
    causal_conv1d_update = staticmethod(kernel)
    chunk_gated_delta_rule = staticmethod(kernel)
    recurrent_gated_delta_rule = staticmethod(kernel)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.layer = DeltaLayer()
        self.model = SimpleNamespace(modules=lambda: [self.layer])
        self.qwen = SimpleNamespace(
            Qwen3_5GatedDeltaNet=DeltaLayer,
            causal_conv1d_fn=kernel,
            causal_conv1d_update=kernel,
            chunk_gated_delta_rule=kernel,
            fused_recurrent_gated_delta_rule=kernel,
        )

    def audit(self):
        with patch.object(fast_path, "require_fast_path", return_value=self.qwen):
            return fast_path.FastPathAudit(self.model)

    def test_requires_accelerated_model_bindings(self):
        self.layer.chunk_gated_delta_rule = lambda: None
        with self.assertRaisesRegex(RuntimeError, "fallback binding"):
            self.audit()

    def test_availability_alone_does_not_prove_execution(self):
        audit = self.audit()
        with self.assertRaisesRegex(RuntimeError, "did not execute"):
            audit.begin_inference()
            audit.verify_inference()

    def test_counts_successful_real_calls_and_preserves_return_value(self):
        audit = self.audit()
        audit.begin_inference()
        self.assertEqual(self.layer.causal_conv1d_fn(), "result")
        self.assertEqual(self.layer.chunk_gated_delta_rule(), "result")
        audit.verify_inference()
        self.assertEqual(audit.context.calls["causal_conv1d_fn"], 1)
        with self.assertRaisesRegex(RuntimeError, "did not execute"):
            audit.begin_inference()
            audit.verify_inference()

    def test_parallel_requests_have_separate_audit_counters(self):
        audit = self.audit()
        barrier = threading.Barrier(2)

        def request():
            audit.begin_inference()
            barrier.wait(timeout=5)
            self.layer.causal_conv1d_fn()
            self.layer.chunk_gated_delta_rule()
            audit.verify_inference()
            return dict(audit.context.calls)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: request(), range(2)))
        self.assertEqual(results, [{"causal_conv1d_fn": 1, "chunk_gated_delta_rule": 1}] * 2)


if __name__ == "__main__":
    unittest.main()
