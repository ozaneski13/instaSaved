from igsaved.config import Config
from igsaved.transcribe import Transcriber, prepare_cuda_dlls


def test_attempt_order_cuda_then_cpu():
    cfg = Config.load(None)
    assert Transcriber(cfg)._build_attempts() == [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "float32"), ("cpu", "int8")]


def test_attempt_order_cpu_only():
    cfg = Config.load(None)
    cfg.raw["whisper"]["device"] = "cpu"
    cfg.raw["whisper"]["compute_type"] = "int8"
    assert Transcriber(cfg)._build_attempts() == [("cpu", "int8")]


def test_prepare_cuda_dlls_returns_list():
    assert isinstance(prepare_cuda_dlls(), list)
