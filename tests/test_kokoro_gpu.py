"""Kokoro on the GPU: the helper process's pipe protocol (no GPU needed), the CPU fallback, the
STFT-as-convolution rewrite, and (when `doty gpu-voice` set this PC up) the real thing."""

import io
import os
import struct
import subprocess
import sys

import numpy as np
import pytest

from doty import kokoro, kokoro_gpu
from doty.kokoro import KokoroError


class FakeSession:
    def __init__(self) -> None:
        self.feeds = []

    def get_providers(self):
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def run(self, _names, feeds):
        self.feeds.append(feeds)
        if feeds["input_ids"].size == 3:
            raise RuntimeError("bad shape")
        return [np.arange(feeds["input_ids"].size * 10, dtype=np.float32).reshape(1, -1) * feeds["speed"][0]]


class Pipe:
    """stdin/stdout for the worker: bytes in, bytes out."""

    def __init__(self, data: bytes = b"") -> None:
        self.buffer = io.BytesIO(data)


def request(ids, speed=1.0):
    ids = np.array(ids, np.int64)
    style = np.full(256, 0.5, np.float32)
    return struct.pack("<IIf", ids.size, style.size, speed) + ids.tobytes() + style.tobytes()


def test_worker_answers_each_request_and_reports_errors(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(kokoro_gpu, "_cuda_session", lambda path: fake)
    stdin, stdout = Pipe(request([0, 5, 6, 7, 0], 2.0) + request([0, 1, 0])), Pipe()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert kokoro_gpu.main(["model.onnx"]) == 0  # exits when its input ends
    out = stdout.buffer.getvalue()
    assert out.startswith(b"ok CUDAExecutionProvider\n")
    body = out[len(b"ok CUDAExecutionProvider\n"):]
    (n,) = struct.unpack("<i", body[:4])
    audio = np.frombuffer(body[4:4 + 4 * n], np.float32)
    assert n == 50 and audio[1] == 2.0  # 5 ids x 10 samples, scaled by speed
    rest = body[4 + 4 * n:]
    (bad, size) = struct.unpack("<iI", rest[:8])
    assert bad == -1 and b"bad shape" in rest[8:8 + size]
    assert fake.feeds[0]["style"].shape == (1, 256) and fake.feeds[0]["input_ids"].shape == (1, 5)


def test_worker_reports_a_failed_start(monkeypatch):
    def broken(path):
        raise RuntimeError("CUDA isn't usable")

    monkeypatch.setattr(kokoro_gpu, "_cuda_session", broken)
    stdout = Pipe()
    monkeypatch.setattr(sys, "stdin", Pipe())
    monkeypatch.setattr(sys, "stdout", stdout)
    assert kokoro_gpu.main(["model.onnx"]) == 1
    assert stdout.buffer.getvalue().startswith(b"error RuntimeError(\"CUDA isn't usable\")")


def test_parent_side_reads_audio_and_errors():
    s = object.__new__(kokoro_gpu.GpuSession)
    import threading

    s._lock = threading.Lock()
    audio = np.linspace(-1, 1, 7, dtype=np.float32)
    msg = b"RuntimeError('oops')"

    class Proc:
        stdin = io.BytesIO()
        stdout = io.BytesIO(struct.pack("<i", 7) + audio.tobytes() + struct.pack("<iI", -1, len(msg)) + msg)

    s._proc = Proc()
    feeds = {"input_ids": np.array([[0, 3, 0]]), "style": np.zeros((1, 256), np.float32),
             "speed": np.array([1.1], np.float32)}
    out = s.run(None, feeds)
    assert np.array_equal(out[0].ravel(), audio)
    sent = Proc.stdin.getvalue()
    assert struct.unpack("<IIf", sent[:12])[:2] == (3, 256)
    with pytest.raises(KokoroError, match="oops"):
        s.run(None, feeds)
    with pytest.raises(KokoroError, match="lost the Kokoro GPU helper"):
        s.run(None, feeds)  # nothing more to read: the helper is gone


def test_gpu_failure_falls_back_to_the_cpu(monkeypatch, tmp_path):
    import onnxruntime

    made = []

    class CpuSession:
        def __init__(self, path, so, providers):
            made.append(providers)

    def broken(path):
        raise KokoroError("Kokoro couldn't use the GPU (no driver)")

    monkeypatch.setattr(kokoro_gpu, "available", lambda: True)
    monkeypatch.setattr(kokoro_gpu, "GpuSession", broken)
    monkeypatch.setattr(onnxruntime, "InferenceSession", CpuSession)
    session, device = kokoro._open_session(tmp_path / "model.onnx", 2, "auto")
    assert device == "CPU" and made == [["CPUExecutionProvider"]]
    kokoro._close_session(tmp_path / "model.onnx", 2, "auto")
    with pytest.raises(KokoroError, match="no driver"):
        kokoro._open_session(tmp_path / "model.onnx", 2, "gpu")  # asked for the GPU explicitly
    assert kokoro._sessions == {}


def test_cpu_setting_never_starts_the_helper(monkeypatch, tmp_path):
    import onnxruntime

    monkeypatch.setattr(kokoro_gpu, "available", lambda: True)
    monkeypatch.setattr(kokoro_gpu, "GpuSession", lambda path: pytest.fail("started the GPU helper"))
    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda path, so, providers: object())
    assert kokoro._open_session(tmp_path / "model.onnx", 2, "cpu")[1] == "CPU"
    kokoro._close_session(tmp_path / "model.onnx", 2, "cpu")


GPU_READY = (kokoro_gpu.GPU_DIR / "onnx").is_dir()


@pytest.mark.skipif(not GPU_READY, reason="the GPU voice runtime isn't set up (python -m doty gpu-voice)")
def test_stft_rewrite_matches_onnx_stft():
    """Runs with the GPU folder's onnx package, in its own process (it can't share this one)."""
    code = r'''
import numpy as np, onnxruntime as ort
from onnx import helper, numpy_helper, TensorProto
from doty.kokoro_gpu import stft_as_conv
win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(20) / 20)).astype(np.float32)
g = helper.make_graph([helper.make_node("STFT", ["x", "step", "win", "len"], ["y"], onesided=1, name="s")], "g",
    [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, None])], [helper.make_tensor_value_info("y", TensorProto.FLOAT, None)],
    [numpy_helper.from_array(np.array(5, np.int64), "step"), numpy_helper.from_array(win, "win"),
     numpy_helper.from_array(np.array(20, np.int64), "len")])
m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 20)], ir_version=9)
x = np.random.default_rng(1).standard_normal((1, 900)).astype(np.float32)
ref = ort.InferenceSession(m.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
m2 = stft_as_conv(m)
assert [n.op_type for n in m2.graph.node] == ["Unsqueeze", "Conv", "Reshape", "Transpose"]
out = ort.InferenceSession(m2.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
assert out.shape == ref.shape, (out.shape, ref.shape)
print(float(np.abs(out - ref).max()))
'''
    env = dict(os.environ, PYTHONPATH=str(kokoro_gpu.GPU_DIR))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert float(r.stdout.strip().splitlines()[-1]) < 1e-4


@pytest.mark.skipif(not (kokoro_gpu.available() and kokoro.installed(kokoro.MODELS_DIR)),
                    reason="needs `python -m doty gpu-voice` and the downloaded Kokoro model")
def test_real_kokoro_on_the_gpu():
    k = kokoro.Kokoro(folder=kokoro.MODELS_DIR, device="gpu")
    k.load()
    try:
        assert k.device == "GPU"
        session = k._session
        pcm = b"".join(k.synth("Bounty runes in fifteen seconds.", "af_heart"))
        assert 1.0 < len(pcm) / 2 / kokoro.RATE < 3.5
    finally:
        k.close()
    assert session._proc is None  # the helper process was shut down
