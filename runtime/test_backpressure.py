import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from app import (
    CLEF_MAX_QUEUE_DEPTH,
    MAX_BATCH_PADDED_TOKENS,
    MAX_BATCH_SIZE,
    MicroBatchQueue,
    QueueItem,
    calculate_retry_after,
    estimate_wait_seconds,
)
from starlette.responses import JSONResponse


class DummyRequest:
    def __init__(self, data: bytes):
        self.data = data

    async def stream(self):
        yield self.data


class BackpressureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset queue and state before each test
        app.batch_queue = MicroBatchQueue()
        app.in_flight_batches = 0
        app.avg_batch_seconds = 0.25
        app.avg_batch_seconds_image = 1.5

    def test_dynamic_retry_after_calculation(self):
        # With 0 queued items and 0 in flight:
        self.assertEqual(estimate_wait_seconds(has_images=False), 0.0)
        self.assertEqual(calculate_retry_after(has_images=False), 1)

        # With 1 item and in_flight=1:
        # (ceil(1/8) + 1) * 0.25 = 2 * 0.25 = 0.5s -> math.ceil = 1
        with patch.object(app, "in_flight_batches", 1):
            app.batch_queue.put_nowait(QueueItem(None, {}, None))
            self.assertEqual(estimate_wait_seconds(has_images=False), 0.5)
            self.assertEqual(calculate_retry_after(has_images=False), 1)

        # With 64 items and in_flight=1 (text):
        # (ceil(64/8) + 1) * 0.25 = 9 * 0.25 = 2.25s -> math.ceil = 3
        with patch.object(app, "in_flight_batches", 1):
            for _ in range(63):
                app.batch_queue.put_nowait(QueueItem(None, {}, None))
            self.assertEqual(estimate_wait_seconds(has_images=False), 2.25)
            self.assertEqual(calculate_retry_after(has_images=False), 3)

        # Clamping check: high wait clamped to 30
        with patch.object(app, "avg_batch_seconds", 10.0), patch.object(app, "in_flight_batches", 5):
            self.assertEqual(calculate_retry_after(has_images=False), 30)

    async def test_stage_a_fast_admission_control_queue_full(self):
        # Fill queue to CLEF_MAX_QUEUE_DEPTH
        for _ in range(CLEF_MAX_QUEUE_DEPTH):
            app.batch_queue.put_nowait(QueueItem(None, {}, None))

        payload = {
            "model": app.MODEL_NAME,
            "state": "burst-test",
            "questions": {"q": {"type": "noul"}},
        }
        req = DummyRequest(json.dumps(payload).encode())
        response = await app.systemone(req)

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        data = json.loads(response.body.decode())
        self.assertIn("Server busy; retry after", data["detail"])
        self.assertGreaterEqual(data["retry_after_seconds"], 1)
        self.assertEqual(data["queue_depth"], CLEF_MAX_QUEUE_DEPTH)

    async def test_stage_a_fast_admission_control_wait_time_exceeded(self):
        # Set average batch seconds high so wait time exceeds 15.0s even with few items
        with patch.object(app, "avg_batch_seconds", 20.0), patch.object(app, "in_flight_batches", 1):
            payload = {
                "model": app.MODEL_NAME,
                "state": "burst-test",
                "questions": {"q": {"type": "noul"}},
            }
            req = DummyRequest(json.dumps(payload).encode())
            response = await app.systemone(req)
            self.assertEqual(response.status_code, 429)
            self.assertIn("Retry-After", response.headers)

    async def test_stage_b_queue_deadline_expiration_and_cancellation(self):
        # Set queue wait timeout very short to trigger Stage B
        payload = {
            "model": app.MODEL_NAME,
            "state": "timeout-test",
            "questions": {"q": {"type": "noul"}},
        }
        req = DummyRequest(json.dumps(payload).encode())

        # No worker running so request will expire in queue
        with patch.object(app, "CLEF_MAX_QUEUE_WAIT_SECONDS", 0.05), patch.object(app, "ensure_worker_started", lambda: None):
            response = await app.systemone(req)
            self.assertEqual(response.status_code, 429)
            self.assertIn("Retry-After", response.headers)
            body = json.loads(response.body.decode())
            self.assertIn("Server busy; retry after", body["detail"])

            # Remove expired records immediately so cancelled payloads cannot
            # accumulate behind a long-running batch.
            self.assertEqual(len(app.batch_queue._deque), 0)
            self.assertEqual(app.batch_queue.qsize(), 0)

            # Enqueue a valid non-cancelled item and verify get_batch skips the cancelled item
            valid_item = QueueItem(SimpleNamespace(input_ids=[0] * 10), {"state": "valid"}, None)
            app.batch_queue.put_nowait(valid_item)
            batch = await app.batch_queue.get_batch(MAX_BATCH_SIZE, MAX_BATCH_PADDED_TOKENS)
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0], valid_item)

    async def test_micro_batch_queue_sorting_and_overflow(self):
        queue = MicroBatchQueue()
        # Create items with different token lengths: 100, 500, 50, 300
        lengths = [100, 500, 50, 300]
        for idx, length in enumerate(lengths):
            rec = SimpleNamespace(input_ids=[0] * length)
            queue.put_nowait(QueueItem(rec, {"id": idx}, None))

        # Drain batch
        batch = await queue.get_batch(max_batch_size=4, max_padded_tokens=8192)
        # Should be sorted ascending by length: 50, 100, 300, 500
        returned_lengths = [len(it.record.input_ids) for it in batch]
        self.assertEqual(returned_lengths, [50, 100, 300, 500])

    async def test_burst_load_handling_50_to_100_requests(self):
        # Configure a mock inference runner
        app.inference = lambda p: {"result": "ok", "state": p.get("state")}
        app.collate_records_fn = None
        app.systemone_answer_fn = None

        payload = {
            "model": app.MODEL_NAME,
            "state": "burst",
            "questions": {"q": {"type": "noul"}},
        }
        body = json.dumps(payload).encode()

        # Send 75 concurrent requests
        tasks = [app.systemone(DummyRequest(body)) for _ in range(75)]
        responses = await asyncio.gather(*tasks)

        # Check outcomes: each response is either a successful dict result or a 429 JSONResponse
        successes = 0
        rate_limits = 0
        for resp in responses:
            if isinstance(resp, dict) and resp.get("result") == "ok":
                successes += 1
            elif isinstance(resp, JSONResponse) and resp.status_code == 429:
                rate_limits += 1
                self.assertIn("Retry-After", resp.headers)
            else:
                self.fail(f"Unexpected response type: {resp}")

        self.assertEqual(successes + rate_limits, 75)
        self.assertGreater(successes, 0)


if __name__ == "__main__":
    unittest.main()
