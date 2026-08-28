# -*- coding: utf-8 -*-
"""拾景纸刊 v10 的离线契约测试；不触发网页或图像生成。"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v10_visual_batch as v10


def complete_card_text():
    fields = sorted(v10.REQUIRED_VISUAL_FIELDS)
    values = {field: "具体可见方案" for field in fields}
    values["CARD_VERSION"] = "v10.0"
    values["ROUTE"] = "HUMAN_STORY"
    values["CANVAS_DIRECTION"] = "横版，视线向右展开"
    return "\n".join(f"{key}: {value}" for key, value in values.items())


def test_parse_and_validate_complete_card():
    card = v10.parse_card(complete_card_text())
    assert not v10.card_issues(card)


def test_invalid_card_reports_missing_route_and_canvas():
    card = {"ROUTE": "PORTRAIT", "CANVAS_DIRECTION": "自由"}
    issues = v10.card_issues(card)
    assert any(item.startswith("missing=") for item in issues)
    assert "invalid_route=PORTRAIT" in issues
    assert "unclear_canvas=自由" in issues


def test_render_is_direct_and_has_no_final_prompt_stage():
    instruction = v10.build_render_instruction("CARD_VERSION: v10.0\nROUTE: HUMAN_STORY")
    assert "directly generate the finished" in instruction
    assert "Do not reply with a plan" in instruction
    assert "final_prompt" not in instruction


def test_v10_keeps_freedom_and_source_causality():
    card_prompt = v10.CARD_TEMPLATE
    render_prompt = v10.RENDER_TEMPLATE
    assert "不得默认竖版 3:5" in card_prompt
    assert "HERO_TRANSFORMATION" in card_prompt
    assert "SHARED_SKELETON" in card_prompt
    assert "REMOVAL_TEST" in card_prompt
    assert "do not default to a vertical 3:5" in render_prompt
    assert "source-derived skeleton" in render_prompt


def test_no_single_photo_elements_are_baked_into_v10():
    joined = v10.CARD_TEMPLATE + v10.RENDER_TEMPLATE
    forbidden = ("elderly woman", "white hair", "red doorway", "vermilion", "老人", "红门", "白发")
    assert not any(word in joined for word in forbidden)


def test_quality_gate_targets_known_v9_failure_modes():
    text = v10.RENDER_TEMPLATE
    required = (
        "bold rather than timid",
        "quiet field is composed rather than leftover",
        "structural color changes the eye path",
        "does not resemble a reusable photo-on-paper template",
        "correct the image once internally",
    )
    assert all(item in text for item in required)


def test_pipeline_submits_all_renders_before_first_wait_and_downloads_in_order():
    """No real browser: assert the exact concurrency contract with two fake photos."""
    names = (
        "post", "wait_new_conversation", "upload_photo", "send_msg", "wait_design",
        "last_assistant", "state", "wait_cid", "wait_image", "do_download",
    )
    originals = {name: getattr(v10, name) for name in names}
    original_sleep = v10.time.sleep
    render_sends = []
    download_visits = []
    cid_values = iter(("cid-one", "cid-two"))

    def fake_post(endpoint, payload):
        if endpoint == "/navigate" and "/c/" in payload["url"]:
            download_visits.append(payload["url"])
        return {"ok": True}

    def fake_send(message, **kwargs):
        if message.startswith("Use the uploaded photograph now"):
            render_sends.append(message)
        return True

    def fake_wait_image(prev_gen, timeout):
        assert len(render_sends) == 2, "pipeline waited before every render was submitted"
        return True

    def fake_download(path, prev_gen):
        Path(path).write_bytes(b"png")
        return True

    try:
        v10.post = fake_post
        v10.wait_new_conversation = lambda timeout: True
        v10.upload_photo = lambda photo: True
        v10.send_msg = fake_send
        v10.wait_design = lambda previous, timeout: True
        v10.last_assistant = complete_card_text
        v10.state = lambda: {"gen": 0}
        v10.wait_cid = lambda timeout=20: next(cid_values)
        v10.wait_image = fake_wait_image
        v10.do_download = fake_download
        v10.time.sleep = lambda seconds: None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            photos = [root / "one.jpg", root / "two.jpg"]
            for photo in photos:
                photo.write_bytes(b"source")
            out = root / "out"
            ok, fail = v10.process_pipeline(photos, out, "_v10")
            assert (ok, fail) == (2, 0)
            assert download_visits == [
                "https://chatgpt.com/c/cid-one",
                "https://chatgpt.com/c/cid-two",
            ]
            manifest = json.loads((out / ".v10-pipeline-manifest.json").read_text(encoding="utf-8"))
            assert manifest["phase"] == "complete"
            assert [job["status"] for job in manifest["jobs"]] == ["downloaded", "downloaded"]
            for artifact_dir in out.glob("*-artifacts"):
                assert len(list(artifact_dir.iterdir())) == 6
    finally:
        for name, value in originals.items():
            setattr(v10, name, value)
        v10.time.sleep = original_sleep


def test_manifest_is_atomic_and_resume_readable():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "manifest.json"
        value = {"version": "v10-pipeline-1", "phase": "download", "jobs": [{"cid": "abc"}], "failures": []}
        v10.write_json(path, value)
        assert v10.read_manifest(path) == value
        assert not path.with_suffix(".json.tmp").exists()


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"✅ {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
