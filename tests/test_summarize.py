import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from igsaved.config import Config
from igsaved.summarize import (
    OpenAICompatibleProvider,
    ProviderUnavailable,
    build_user_prompt,
    make_provider,
    summarize,
)


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, images=()):
        self.calls.append((system, user))
        return "  Özet metni.  "


def test_summarize_uses_provider_and_strips():
    p = FakeProvider()
    out = summarize(p, "creator", "caption here", "transkript burada", "tr")
    assert out == "  Özet metni.  " or out.strip() == "Özet metni."
    system, user = p.calls[0]
    assert "Türkçe" in system
    assert "@creator" in user and "caption here" in user and "transkript burada" in user and "dil: tr" in user


def test_build_user_prompt_handles_missing():
    user = build_user_prompt("video", None, None, "", None)
    assert "(yok)" in user and "bilinmiyor" in user and "(konuşma yok)" in user


class _Handler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _Handler.received.append((self.path, self.headers.get("Authorization"), body))
        payload = {"choices": [{"message": {"role": "assistant", "content": f"ÖZET: {body['model']}"}}]}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_openai_compatible_provider_posts_chat_completions(stub_server):
    provider = OpenAICompatibleProvider(model="gemma4:latest", base_url=stub_server + "/", api_key="sk-test", max_tokens=64)
    out = provider.complete("sys", "usr")
    assert out == "ÖZET: gemma4:latest"
    path, auth, body = _Handler.received[-1]
    assert path == "/v1/chat/completions" and auth == "Bearer sk-test"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["max_tokens"] == 64


def test_make_provider_requires_key_or_base_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config.load(None)
    cfg.raw["llm"]["provider"] = "anthropic"
    with pytest.raises(ProviderUnavailable):
        make_provider(cfg)
    cfg.raw["llm"]["provider"] = "openai_compatible"
    cfg.raw["llm"]["base_url"] = ""
    with pytest.raises(ProviderUnavailable):
        make_provider(cfg)
    cfg.raw["llm"]["base_url"] = "http://localhost:11434/v1"
    cfg.raw["llm"]["model"] = "gemma4:latest"
    assert make_provider(cfg).name == "openai_compatible"


def test_config_merge_and_paths(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"llm": {"provider": "openai_compatible", "base_url": "http://x/v1"}, "data_dir": "d"}), encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.get("llm", "provider") == "openai_compatible"
    assert cfg.get("llm", "max_tokens") == 2048
    assert cfg.data_dir == tmp_path / "d"
    assert cfg.db_path == tmp_path / "d" / "state.db"


def test_claude_code_provider_invokes_cli(monkeypatch):
    import subprocess
    from igsaved.summarize import ClaudeCodeProvider
    calls = {}

    class R:
        returncode = 0
        stdout = "  Özet çıktısı.\n"
        stderr = ""

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["input"] = kw.get("input")
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    p = ClaudeCodeProvider(model="sonnet", exe="C:/fake/claude.cmd", effort="low")
    assert p.complete("SYS", "USER") == "Özet çıktısı."
    assert calls["cmd"][:2] == ["C:/fake/claude.cmd", "-p"] and "--bare" not in calls["cmd"] and "--tools" in calls["cmd"]
    assert "--system-prompt" in calls["cmd"] and "SYS" in calls["cmd"] and "--model" in calls["cmd"]
    assert calls["input"] == "USER"


def test_claude_code_provider_not_logged_in(monkeypatch):
    import subprocess
    from igsaved.summarize import ClaudeCodeProvider, ProviderUnavailable

    class R:
        returncode = 1
        stdout = "Not logged in · Please run /login"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: R())
    p = ClaudeCodeProvider(exe="C:/fake/claude.cmd")
    with pytest.raises(ProviderUnavailable):
        p.complete("s", "u")
