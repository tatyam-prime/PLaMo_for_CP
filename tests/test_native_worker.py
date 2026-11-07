"""Native owner scheduling and framing contracts, using no DLL or GPU."""

from __future__ import annotations

import io
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from plamo_cp import native_ipc as ipc
from plamo_cp import native_worker as worker


def frame(key, source="Find an integer [[PLM_0]]."):
    return {"type": "submit", "id": key, "text": source, "parameters": {}}


class FakeBridge:
    def __init__(self, prompt_size=3, *, chunks=None):
        self.prompt_size = prompt_size
        self.chunks = chunks or ["整数 <math-a/>。".encode()]
        self.batches, self.removed, self.samples, self.syncs = [], [], [], 0
        self.counts = {}

    def tokenize(self, value, **_options):
        if value in {"<|plamo:tag|>", "<|plamo:bos|>"}:
            return [999 if value.endswith("tag|>") else 998]
        return [1] * (self.prompt_size if value.startswith("<|plamo:bos|>") else 1)

    def decode(self, entries):
        self.batches.append(list(entries))

    def sample(self, index):
        _token, _position, sequence, flag = self.batches[-1][index]
        assert flag == 1
        self.samples.append((index, sequence))
        count = self.counts.get(sequence, 0)
        self.counts[sequence] = count + 1
        return 100 + count if count < len(self.chunks) else 0

    def piece(self, token):
        return self.chunks[token - 100]

    def is_eog(self, token):
        return token == 0

    def remove(self, sequence):
        self.removed.append(sequence)
        self.counts.pop(sequence, None)

    def synchronize(self):
        self.syncs += 1


def finish_owner(owner, maximum=100):
    for _ in range(maximum):
        if not owner.active and not owner.commands.pending and owner.waiting is None:
            return
        owner.step()
    raise AssertionError("The fake owner failed to drain")


def test_framing_survives_short_reads_and_writes_and_rejects_oversized_or_partial_frames():
    class Short(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 3))

        def write(self, data):
            return super().write(data[:2])

    stream = Short()
    value = {"type": "result", "id": "a", "text": "数式 [[PLM_0]]"}
    ipc.write_frame(stream, value)
    stream.seek(0)
    assert ipc.read_frame(stream) == value and ipc.read_frame(stream) is None
    for raw in (b"\x00", struct.pack("!I", ipc.MAX_FRAME_BYTES + 1), b"\x00\x00\x00\x05{}"):
        with pytest.raises(ipc.ProtocolError):
            ipc.read_frame(io.BytesIO(raw))


@pytest.mark.parametrize(
    "raw",
    [b"[]", b'{"type":"close","type":"submit"}', b'{"type":"bad","x":NaN}', b'{"type":1}', b"\xff"],
)
def test_framing_rejects_ambiguous_or_non_json_values(raw):
    with pytest.raises(ipc.ProtocolError):
        ipc.read_frame(io.BytesIO(struct.pack("!I", len(raw)) + raw))


def test_strict_commands_bound_ids_queue_levels_and_do_not_accept_extra_paths():
    commands = worker.Commands()
    emitted = []
    commands.receive(frame("good"), emitted.append)
    for invalid in (
        frame("good"),
        frame("bad\n"),
        {**frame("other"), "path": "C:/injected.dll"},
        {"type": "quiet", "id": "q", "level": True},
        {"type": "quiet", "id": "q", "level": 8},
        {"type": "execute", "id": "q"},
    ):
        with pytest.raises(ipc.ProtocolError):
            commands.receive(invalid, emitted.append)
    commands.receive({"type": "quiet", "id": "q", "level": 7}, emitted.append)
    assert emitted == [{"type": "quiet_applied", "id": "q", "level": 7}]
    assert len(commands.pending) == 1


def test_child_accepts_the_full_api_source_limit_and_rejects_the_next_character():
    commands = worker.Commands()
    commands.receive(frame("legal", "a" * 8000), lambda _: None)
    assert len(commands.pending[0]["text"]) == 8000
    with pytest.raises(ipc.ProtocolError):
        commands.receive(frame("too_long", "a" * 8001), lambda _: None)


def test_owner_emits_canonical_preview_and_result_with_incremental_utf8():
    raw = "整数 <math-a/>。".encode()
    bridge = FakeBridge(chunks=[raw[:1], raw[1:2], raw[2:]])
    commands, emitted = worker.Commands(), []
    commands.receive(frame("a"), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    finish_owner(owner)
    result = [value for value in emitted if value["type"] == "result"]
    assert result == [{"type": "result", "id": "a", "text": "整数 [[PLM_0]]。"}]
    assert owner.reserved == 0 and len(owner.slots) == 32 and commands.identifiers == set()
    assert bridge.removed == [0] and bridge.syncs == 1


def test_all_32_logit_rows_stay_with_their_sequences_and_generation_precedes_prefill():
    bridge, commands, emitted = FakeBridge(prompt_size=300), worker.Commands(), []
    for index in range(32):
        commands.receive(frame(str(index)), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    owner.step()
    assert len(owner.active) == 32 and len(bridge.batches[0]) == 256
    owner.step()  # Finish the first prompt, making its first generated token ready.
    owner.step()  # That token precedes the next prompt's 255-token prefill chunk.
    assert bridge.batches[2][0][2:] == (0, 1)
    assert all(entry[2] == 1 for entry in bridge.batches[2][1:])
    finish_owner(owner, maximum=200)
    assert len([value for value in emitted if value["type"] == "result"]) == 32
    assert all(1 <= len(batch) <= 256 for batch in bridge.batches)
    assert {sequence for _, sequence in bridge.samples} == set(range(32))


def test_weighted_admission_and_cancel_free_only_the_target_kv_before_refill():
    bridge, commands, emitted = FakeBridge(prompt_size=20000), worker.Commands(), []
    commands.receive(frame("a"), emitted.append)
    commands.receive(frame("b"), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    owner.step()
    assert owner.reserved == 20512 and len(owner.active) == 1 and owner.waiting.key == "b"
    commands.receive({"type": "cancel", "id": "a"}, emitted.append)
    owner.step()
    assert bridge.removed == [0] and bridge.syncs == 1
    assert len(owner.active) == 1 and next(iter(owner.active.values())).key == "b"
    assert owner.reserved == 20512 and {value["type"] for value in emitted} == {"cancelled"}


def test_cancel_one_sequence_leaves_other_sequence_output_and_slot_intact():
    bridge, commands, emitted = FakeBridge(chunks=[b"text ", b"<math-a/>"]), worker.Commands(), []
    for key in ("a", "b"):
        commands.receive(frame(key), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    owner.step()
    commands.receive({"type": "cancel", "id": "a"}, emitted.append)
    finish_owner(owner)
    assert [value for value in emitted if value["type"] == "cancelled"] == [
        {"type": "cancelled", "id": "a"}
    ]
    assert [value for value in emitted if value["type"] == "result"] == [
        {"type": "result", "id": "b", "text": "text [[PLM_0]]"}
    ]


def test_bad_final_is_an_uncacheable_segment_error_and_other_sequences_continue():
    bridge, commands, emitted = FakeBridge(chunks=["識別子欠落。".encode()]), worker.Commands(), []
    commands.receive(frame("bad"), emitted.append)
    commands.receive(frame("good", "A plain sentence."), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    finish_owner(owner)
    assert [(value["type"], value["id"]) for value in emitted if value["type"] != "preview"] == [
        ("error", "bad"),
        ("result", "good"),
    ]


@pytest.mark.parametrize("level", range(1, 8))
def test_native_duty_uses_the_same_logarithmic_targets_and_20ms_checks(monkeypatch, level):
    clock = SimpleNamespace(value=0.0)
    waits = []

    class Condition:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            clock.value += seconds

    commands = worker.Commands(level)
    commands.condition = Condition()
    monkeypatch.setattr(worker, "time", SimpleNamespace(perf_counter=lambda: clock.value))
    commands.wait_duty(0.25)
    assert sum(waits) == pytest.approx(0.25 * (5 ** (level / 7) - 1))
    assert all(0 < value <= 0.02 for value in waits)


def test_live_quiet_off_or_cancel_wakes_a_long_gpu_duty_wait():
    for action in ("off", "cancel"):
        commands = worker.Commands(7)
        entered = threading.Event()
        original = commands.condition.wait

        def wait(seconds, entered=entered, original=original):
            entered.set()
            return original(seconds)

        commands.condition.wait = wait
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(commands.wait_duty, 0.25)
            assert entered.wait(1)
            with commands.condition:
                if action == "off":
                    commands.quiet_level = 0
                else:
                    commands.cancelled.add("a")
                commands.condition.notify_all()
            assert future.result(timeout=0.2) is None


def test_all_32_output_rows_produce_their_own_greedy_sequence():
    class DistinctBridge(FakeBridge):
        def sample(self, index):
            sequence = self.batches[-1][index][2]
            count = self.counts.get(sequence, 0)
            self.counts[sequence] = count + 1
            return 1000 + sequence * 100 + count if count < 40 else 0

        def piece(self, token):
            return f"訳{(token - 1000) // 100}。".encode()

    bridge, commands, emitted = DistinctBridge(prompt_size=1), worker.Commands(), []
    for index in range(32):
        commands.receive(frame(str(index), "A sentence."), emitted.append)
    owner = worker.Owner(bridge, commands, emitted.append)
    finish_owner(owner, maximum=100)
    results = {value["id"]: value["text"] for value in emitted if value["type"] == "result"}
    assert results == {str(index): f"訳{index}。" * 40 for index in range(32)}
    assert any(sum(entry[3] for entry in batch) == 32 for batch in bridge.batches)


def test_control_stop_tokens_never_enter_the_final_translation_or_preview():
    class StopBridge(FakeBridge):
        def sample(self, index):
            sequence = self.batches[-1][index][2]
            count = self.counts.get(sequence, 0)
            self.counts[sequence] = count + 1
            return 100 if count == 0 else 999

        def piece(self, token):
            return b"<|plamo:tag|>" if token == 999 else "訳文。".encode()

    bridge, commands, emitted = StopBridge(), worker.Commands(), []
    commands.receive(frame("a", "A sentence."), emitted.append)
    finish_owner(worker.Owner(bridge, commands, emitted.append))
    assert emitted[-1] == {"type": "result", "id": "a", "text": "訳文。"}
    assert all("<|plamo:" not in value.get("text", "") for value in emitted)
