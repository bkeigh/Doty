"""Kokoro on an NVIDIA GPU (source checkouts): about 0.15 s per line instead of 0.6–1.1 s on the CPU.

Why a helper process:
- **Two builds, one package:** the GPU build of ONNX Runtime (`onnxruntime-gpu`) and the CPU build
  faster-whisper requires are the same Python package and can't be installed side by side.
- **A separate folder:** `doty gpu-voice` puts the GPU build and its CUDA 12 libraries in
  `<venv>/kokoro-gpu`.
- **Over pipes:** Doty runs Kokoro in a small child process that imports from that folder. It sends
  token ids and a voice style in and gets audio back. `GpuSession` looks like an
  `onnxruntime.InferenceSession` to `kokoro.Kokoro`.

Why a rewritten model:
- **The slow step:** the model's one STFT (20-sample Hann window, hop 5) has no CUDA kernel, so ONNX
  Runtime ran it on the CPU (~140 ms a line).
- **The fix:** the worker swaps it, in memory, for the same transform as a strided convolution
  (exact to ~1e-5), which runs on the GPU.
- **Not bit-identical:** the model takes the phase of near-silent bins as an input, so its output
  isn't bit-identical to the CPU path. Whisper understands both equally (mean log-prob -0.365 vs
  -0.356 over 24 lines).

Measured on an RTX 4070 Ti: 120–190 ms a line. The installed Doty.exe always uses the CPU (no 2 GB
of CUDA in the installer).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import sysconfig
import threading
from pathlib import Path

from .config import FROZEN, LOG_DIR

GPU_DIR = Path(sys.prefix) / "kokoro-gpu"
# onnxruntime-gpu 1.26 is the last PyPI build for CUDA 12 (1.27+ need CUDA 13). cuBLAS and cuDNN
# come from Doty's [gpu] extra, which Whisper already uses.
# (numpy, protobuf and flatbuffers come from the venv; the CPU onnxruntime needs them too.)
PACKAGES = ["onnxruntime-gpu==1.26.0", "nvidia-cuda-runtime-cu12", "nvidia-cufft-cu12", "nvidia-nvjitlink-cu12",
            "onnx==1.23.2", "ml_dtypes"]
SHARED = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]  # only if the [gpu] extra isn't installed
START_TIMEOUT_S = 60.0


def _site_nvidia() -> Path:
    return Path(sysconfig.get_paths()["purelib"]) / "nvidia"


def _dll_dirs() -> list[Path]:
    return list((GPU_DIR / "nvidia").glob("*/bin")) + list(_site_nvidia().glob("*/bin"))


def available() -> bool:
    """The GPU runtime is set up here and an NVIDIA driver is present. Cheap: no CUDA calls."""
    if FROZEN or sys.platform != "win32" or not (GPU_DIR / "onnxruntime").is_dir():
        return False
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    has_driver = (windir / "System32" / "nvcuda.dll").is_file()
    names = {p.name.lower() for d in _dll_dirs() for p in d.glob("*.dll")}
    return has_driver and {"cublas64_12.dll", "cudnn64_9.dll", "cudart64_12.dll", "cufft64_11.dll"} <= names


def install(log=print) -> int:
    """`doty gpu-voice`: put the GPU runtime in <venv>/kokoro-gpu (~420 MB, more without [gpu])."""
    pkgs = list(PACKAGES)
    if not (_site_nvidia() / "cudnn").is_dir() or not (_site_nvidia() / "cublas").is_dir():
        pkgs += SHARED
    log(f"Installing the GPU voice runtime into {GPU_DIR}:\n  " + " ".join(pkgs))
    return subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", "--target",
                            str(GPU_DIR), *pkgs])


def _python() -> str:
    """python.exe, not pythonw.exe: the child talks over stdin/stdout."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if exe.name.lower() == "pythonw.exe" and console.is_file() else exe)


def _read(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class GpuSession:
    """A Kokoro ONNX session running in the GPU helper process."""

    def __init__(self, model_path: Path) -> None:
        from .kokoro import KokoroError

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(GPU_DIR)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        env["PYTHONIOENCODING"] = "utf-8"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._log = open(LOG_DIR / "kokoro-gpu.log", "ab")
        self._lock = threading.Lock()
        self._proc = subprocess.Popen([_python(), "-m", "doty.kokoro_gpu", str(model_path)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=self._log, env=env,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = self._readline_with_timeout(START_TIMEOUT_S)
        if not line.startswith(b"ok "):
            self.close()
            reason = line.decode("utf-8", "replace").removeprefix("error ").strip() or "it didn't start"
            raise KokoroError(f"Kokoro couldn't use the GPU ({reason[:160]})")
        self.provider = line[3:].decode().strip()

    def _readline_with_timeout(self, timeout: float) -> bytes:
        out: list[bytes] = []
        t = threading.Thread(target=lambda: out.append(self._proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        return out[0] if out else b"error timed out loading the model on the GPU"

    def run(self, _output_names, feeds: dict):
        import numpy as np

        from .kokoro import KokoroError

        ids = np.ascontiguousarray(feeds["input_ids"], dtype=np.int64).ravel()
        style = np.ascontiguousarray(feeds["style"], dtype=np.float32).ravel()
        speed = float(np.asarray(feeds["speed"]).ravel()[0])
        with self._lock:
            try:
                self._proc.stdin.write(struct.pack("<IIf", ids.size, style.size, speed) + ids.tobytes() + style.tobytes())
                self._proc.stdin.flush()
                head = _read(self._proc.stdout, 4)
                if head is None:
                    raise OSError("the GPU helper exited")
                (n,) = struct.unpack("<i", head)
                if n < 0:
                    (size,) = struct.unpack("<I", _read(self._proc.stdout, 4))
                    raise KokoroError(f"Kokoro GPU error: {_read(self._proc.stdout, size).decode('utf-8', 'replace')}")
                audio = np.frombuffer(_read(self._proc.stdout, 4 * n), dtype=np.float32)
            except (OSError, ValueError, struct.error, TypeError) as exc:
                raise KokoroError(f"lost the Kokoro GPU helper ({exc})") from exc
        return [audio.reshape(1, -1)]

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is not None:
            try:
                proc.stdin.close()  # the helper exits when its input ends
                proc.wait(3)
            except Exception:
                proc.kill()
            self._proc = None
        if getattr(self, "_log", None) is not None:
            self._log.close()
            self._log = None


# --------------------------------------------------------------------------- the helper process


def stft_as_conv(model):
    """Replace each ONNX STFT node with Unsqueeze -> Conv (DFT basis x window) -> Reshape -> Transpose."""
    import numpy as np
    from onnx import helper, numpy_helper

    g = model.graph
    inits = {i.name: numpy_helper.to_array(i) for i in g.initializer}
    for idx, node in enumerate(list(g.node)):
        if node.op_type != "STFT":
            continue
        signal, step, window, length = node.input
        hop, win, nfft = int(inits[step]), inits[window].astype(np.float64), int(inits[length])
        bins = nfft // 2 + 1
        t, k = np.arange(nfft), np.arange(bins)[:, None]
        real = win * np.cos(2 * np.pi * k * t / nfft)
        imag = -win * np.sin(2 * np.pi * k * t / nfft)
        imag[0] = 0.0  # DC and Nyquist are exactly real, as in an FFT
        if nfft % 2 == 0:
            imag[-1] = 0.0
        weight = np.concatenate([real, imag])[:, None, :].astype(np.float32)  # [2*bins, 1, nfft]
        p = node.name + "_conv"
        g.initializer.extend([numpy_helper.from_array(weight, p + "_w"),
                              numpy_helper.from_array(np.array([1], np.int64), p + "_axis"),
                              numpy_helper.from_array(np.array([0, 2, bins, -1], np.int64), p + "_shape")])
        new = [helper.make_node("Unsqueeze", [signal, p + "_axis"], [p + "_x"], name=p + "_in"),  # [b, L] -> [b, 1, L]
               helper.make_node("Conv", [p + "_x", p + "_w"], [p + "_y"], strides=[hop], name=p),
               helper.make_node("Reshape", [p + "_y", p + "_shape"], [p + "_r"], name=p + "_reshape"),
               helper.make_node("Transpose", [p + "_r"], [node.output[0]], perm=[0, 3, 2, 1], name=p + "_out")]
        g.node.remove(node)
        for j, n in enumerate(new):
            g.node.insert(idx + j, n)
    return model


def _cuda_session(model_path: Path):
    for d in _dll_dirs():
        os.add_dll_directory(str(d))
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    import numpy as np
    import onnx
    import onnxruntime as ort

    if not Path(ort.__file__).resolve().is_relative_to(GPU_DIR.resolve()):
        raise RuntimeError(f"loaded the CPU onnxruntime from {ort.__file__}")
    ort.set_default_logger_severity(3)
    so = ort.SessionOptions()
    so.log_severity_level = 3
    data = stft_as_conv(onnx.load(str(model_path))).SerializeToString()
    sess = ort.InferenceSession(data, so, providers=[("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}),
                                                     "CPUExecutionProvider"])
    if sess.get_providers()[0] != "CUDAExecutionProvider":
        raise RuntimeError("CUDA isn't usable (check the NVIDIA driver)")
    style = np.zeros((1, 256), np.float32)
    for n in (8, 40):  # the first runs are slow; pay that before Doty speaks
        sess.run(None, {"input_ids": np.zeros((1, n), np.int64), "style": style, "speed": np.ones(1, np.float32)})
    return sess


def main(argv: list[str]) -> int:
    import numpy as np

    out, inp = sys.stdout.buffer, sys.stdin.buffer
    try:
        sess = _cuda_session(Path(argv[0]))
    except Exception as exc:
        out.write(f"error {exc!r}\n".encode("utf-8", "replace"))
        out.flush()
        return 1
    out.write(f"ok {sess.get_providers()[0]}\n".encode())
    out.flush()
    while True:
        head = _read(inp, 12)
        if head is None:
            return 0
        n_ids, n_style, speed = struct.unpack("<IIf", head)
        ids = np.frombuffer(_read(inp, 8 * n_ids), dtype=np.int64).reshape(1, -1)
        style = np.frombuffer(_read(inp, 4 * n_style), dtype=np.float32).reshape(1, -1)
        try:
            audio = sess.run(None, {"input_ids": ids, "style": style, "speed": np.array([speed], np.float32)})[0]
            audio = np.ascontiguousarray(audio, dtype=np.float32).ravel()
            out.write(struct.pack("<i", audio.size) + audio.tobytes())
        except Exception as exc:
            msg = repr(exc).encode("utf-8", "replace")
            out.write(struct.pack("<iI", -1, len(msg)) + msg)
        out.flush()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
