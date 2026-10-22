"""Regressions at the model adapter and request/queue execution boundaries."""

import asyncio
import json
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import app


class Request:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()
        self.disconnected = asyncio.Event()

    async def stream(self):
        yield self.body

    async def receive(self):
        await self.disconnected.wait()
        return {"type": "http.disconnect"}


def payload(**extra):
    return {"model": app.MODEL_NAME, "state": "synthetic", "questions": {"q": {"type": "noul"}}, **extra}


class RuntimeContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = []
        for name, value in {
            "batch_queue": app.MicroBatchQueue(),
            "worker_task": None,
            "in_flight_batches": 0,
            "avg_batch_seconds": 0.001,
            "avg_batch_seconds_image": 0.001,
            "processor": SimpleNamespace(tokenizer=SimpleNamespace(pad_token_id=0)),
            "encode_record_fn": lambda *a, **kw: SimpleNamespace(input_ids=[1] * 16),
            "fast_path": None,
        }.items():
            p = patch.object(app, name, value)
            p.start()
            self.patches.append(p)

    async def asyncTearDown(self):
        if app.worker_task:
            app.worker_task.cancel()
            await app.worker_task
        for p in reversed(self.patches):
            p.stop()

    async def test_running_inference_does_not_expire_at_queue_deadline(self):
        started = threading.Event()
        release = threading.Event()

        def infer(items):
            started.set()
            release.wait(2)
            return [{"result": "ok"} for _ in items]

        with patch.object(app, "CLEF_MAX_QUEUE_WAIT_SECONDS", 0.03), patch.object(app, "run_gpu_batch_sync", infer):
            task = asyncio.create_task(app.systemone(Request(payload())))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.sleep(0.08)
            release.set()
            result = await task
        self.assertEqual(result, {"result": "ok"})

    async def test_disconnect_removes_waiting_work(self):
        request = Request(payload())
        with patch.object(app, "ensure_worker_started", lambda: None):
            task = asyncio.create_task(app.systemone(request))
            for _ in range(100):
                if app.batch_queue.qsize():
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(app.batch_queue.qsize(), 1)
            request.disconnected.set()
            done, _ = await asyncio.wait([task], timeout=0.2)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.fail("HTTP disconnect left the request queued")
            self.assertEqual(app.batch_queue.qsize(), 0)
            self.assertEqual(task.result().status_code, 499)

    async def test_missing_state_and_bad_noul_criteria_are_400(self):
        missing = payload()
        del missing["state"]
        bad_criteria = payload(questions={"q": {"type": "noul", "criteria": [1]}})
        with patch.object(app, "run_gpu_batch_sync", lambda items: [{"unexpected": True}]):
            for value in (missing, bad_criteria):
                with self.subTest(value=value):
                    with self.assertRaises(app.HTTPException) as caught:
                        await app.systemone(Request(value))
                    self.assertEqual(caught.exception.status_code, 400)

    async def test_validation_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def encode(*args, **kwargs):
            started.set()
            release.wait(0.3)
            finished.set()
            return SimpleNamespace(input_ids=[0] * 16)

        with patch.object(app, "encode_record_fn", encode), patch.object(app, "run_gpu_batch_sync", lambda items: [{}]):
            task = asyncio.create_task(app.systemone(Request(payload())))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            # The encoder must still be blocked when the event loop gets control.
            self.assertFalse(finished.is_set(), "CPU encoding blocked the event loop")
            self.assertEqual(app.batch_queue.qsize(), 0)
            release.set()
            await task

    async def test_expired_queued_work_is_never_dispatched(self):
        loop = asyncio.get_running_loop()
        expired = app.QueueItem(SimpleNamespace(input_ids=[0] * 16), payload(), loop.create_future())
        expired.deadline = 0
        live = app.QueueItem(SimpleNamespace(input_ids=[0] * 32), payload(), loop.create_future())
        app.batch_queue.put_nowait(expired)
        app.batch_queue.put_nowait(live)
        batch = await app.batch_queue.get_batch(8, 8192)
        self.assertEqual(batch, [live])
        with self.assertRaises(app.QueueExpired):
            expired.future.result()

    async def test_wait_estimate_accounts_for_padding_and_images_ahead(self):
        for _ in range(8):
            app.batch_queue.put_nowait(app.QueueItem(SimpleNamespace(input_ids=[0] * 2048), payload(), None, True))
        with patch.object(app, "avg_batch_seconds_image", 2):
            self.assertEqual(app.estimate_wait_seconds(has_images=False), 4)

    async def test_context_and_backend_errors_are_distinct(self):
        with patch.object(app, "encode_record_fn", lambda *a, **kw: SimpleNamespace(input_ids=[0] * 2049)):
            with self.assertRaises(app.HTTPException) as caught:
                await app.systemone(Request(payload()))
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(app.batch_queue.qsize(), 0)
        with patch.object(app, "run_gpu_batch_sync", side_effect=RuntimeError("private backend details")):
            with self.assertRaises(app.HTTPException) as caught:
                await app.systemone(Request(payload()))
            self.assertEqual(caught.exception.status_code, 500)
            self.assertEqual(caught.exception.detail, "Internal server error")


class ModelSignatureTest(unittest.TestCase):
    def test_collation_forward_and_per_question_serialization(self):
        """Signatures match both checksum-pinned upstream model releases."""
        seen = []
        question = SimpleNamespace(question_id="q", option_ids=("true", "false"))
        record = SimpleNamespace(input_ids=[1, 2, 3], questions=(question,))
        item = SimpleNamespace(record=record, payload=payload())

        def collate(records, pad_token_id, device):
            self.assertEqual(records, [record])
            self.assertEqual((pad_token_id, device), (7, "cpu"))
            seen.append("collate")
            return {"records": records}

        logits = SimpleNamespace(float=lambda: SimpleNamespace(softmax=lambda axis: SimpleNamespace(tolist=lambda: [0.75, 0.25])))

        class Model:
            def parameters(self):
                return iter([SimpleNamespace(device="cpu")])

            def __call__(self, batch):
                seen.append("forward")
                return [[logits]]

        def answer(question, probabilities):
            self.assertEqual(question, {"type": "noul"})
            seen.append("answer")
            return {"type": "noul", "noul": probabilities["true"]}

        torch = SimpleNamespace(inference_mode=nullcontext, cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict("sys.modules", {"torch": torch}), patch.multiple(
            app, model=Model(), processor=SimpleNamespace(tokenizer=SimpleNamespace(pad_token_id=7)),
            collate_records_fn=collate, systemone_answer_fn=answer, fast_path=None,
        ):
            result = app.run_gpu_batch_sync([item])
        self.assertEqual(seen, ["collate", "forward", "answer"])
        self.assertEqual(result, [{"model": app.MODEL_NAME, "answers": {"q": {"type": "noul", "noul": 0.75}},
                                  "usage": {"input_tokens": 3, "output_tokens": 0}}])
