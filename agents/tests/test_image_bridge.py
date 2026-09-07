import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "image_bridge.py"
SPEC = importlib.util.spec_from_file_location("image_bridge_under_test", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeProvider:
    def __init__(self, image_path):
        self.image_path = image_path

    def is_available(self):
        return True

    def generate(self, prompt, aspect_ratio):
        return {
            "success": True,
            "image": str(self.image_path),
            "model": "gpt-image-test",
            "provider": "openai-codex",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
        }


def test_generate_payload_returns_openai_compatible_base64(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = bridge.generate_payload(
        {"prompt": "minimal black logo", "size": "1:1"},
        provider=FakeProvider(image),
    )

    assert result["provider"] == "openai-codex"
    assert result["data"][0]["b64_json"].startswith("iVBOR")


def test_provider_status_does_not_require_network(tmp_path):
    result = bridge.provider_status(FakeProvider(tmp_path / "unused.png"))

    assert result["authenticated"] is True
    assert result["accounts"] == [{"status": "OK"}]


def test_aspect_ratio_mapping():
    assert bridge._aspect_from_size("1:1") == "square"
    assert bridge._aspect_from_size("9:16") == "portrait"
    assert bridge._aspect_from_size("16:9") == "landscape"
