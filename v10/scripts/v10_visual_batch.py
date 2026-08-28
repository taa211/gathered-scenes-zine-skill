# -*- coding: utf-8 -*-
"""拾景纸刊 v10：视觉决策卡 → 后台生成流水线 → 回访下载归档。

v10 deliberately removes v9's intermediate final_prompt compilation.  The
decision card chooses the source-specific visual mechanism; the second turn
renders from that mechanism directly in the same image-aware conversation.

Usage:
  python -X utf8 scripts/v10_visual_batch.py photo.jpg ... --out OUTPUT_DIR
  python -X utf8 scripts/v10_visual_batch.py photo.jpg ... --out OUTPUT_DIR --single-conversation

Default mode is a single-tab pipeline: each render request is submitted and
left generating in its own persisted conversation while the next photo is
analysed.  After all submissions, conversations are revisited from the first
job onward and downloaded.  Pass --serial only for conservative debugging.
The optional --single-conversation mode is a conservative experiment: all
photos are processed serially in one image-aware conversation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from gpt_batch_direct import (  # noqa: E402
    current_cid,
    do_download,
    eval_js,
    last_assistant,
    make_photo_hook,
    post,
    send_msg,
    state,
    upload_photo,
    wait_design,
    wait_image,
    wait_new_conversation,
)

PLAN_DIR = PROJECT_DIR / "plans"
CARD_TEMPLATE = (PLAN_DIR / "gpt-direct-v10-visual-decision-card.txt").read_text(encoding="utf-8")
RENDER_TEMPLATE = (PLAN_DIR / "gpt-direct-v10-render-from-card.txt").read_text(encoding="utf-8")

ROUTES = {"FIELD_LANDSCAPE", "HUMAN_STORY", "STREET_CITY", "OBJECT_STILL", "ANIMAL_LIFE"}
CARD_RE = re.compile(r"^\s*(?:[-*]\s*)?([A-Z_][A-Z0-9_]*)\s*[:：]\s*(.+?)\s*$")
REQUIRED_VISUAL_FIELDS = {
    "CARD_VERSION", "ROUTE", "PUNCTUM", "CANVAS_DIRECTION", "PHOTO_ROLE",
    "PHOTO_KEEP", "HERO_TRANSFORMATION", "SOURCE_EVIDENCE", "SHARED_SKELETON",
    "CHROMATIC_STRUCTURE", "EYE_PATH", "QUIET_FIELD", "MUST_PROTECT",
    "REMOVAL_TEST", "TEMPLATE_RISK",
}


def parse_card(text: str) -> dict[str, str]:
    card: dict[str, str] = {}
    for line in text.replace("```", "").splitlines():
        match = CARD_RE.match(line)
        if match:
            card[match.group(1).upper()] = match.group(2).strip()
    return card


def card_issues(card: dict[str, str]) -> list[str]:
    issues = []
    missing = sorted(REQUIRED_VISUAL_FIELDS - set(card))
    if missing:
        issues.append("missing=" + ",".join(missing))
    route = card.get("ROUTE", "").upper()
    if route and route not in ROUTES:
        issues.append("invalid_route=" + route)
    canvas = card.get("CANVAS_DIRECTION", "")
    if canvas and not any(word in canvas for word in ("横", "竖", "方", "landscape", "portrait", "square")):
        issues.append("unclear_canvas=" + canvas)
    return issues


def build_render_instruction(card_text: str) -> str:
    return RENDER_TEMPLATE.replace("{{VISUAL_DECISION_CARD}}", card_text.strip())


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    """Atomically persist pipeline state so an interruption can resume safely."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": "v10-pipeline-1", "phase": "submit", "jobs": [], "failures": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("jobs"), list):
            value["version"] = "v10-pipeline-1"
            value.setdefault("failures", [])
            value.setdefault("phase", "submit")
            return value
    except Exception as exc:
        print(f"  ⚠️ pipeline manifest 读取失败，将保留原文件并重新建状态: {exc}", flush=True)
    return {"version": "v10-pipeline-1", "phase": "submit", "jobs": [], "failures": []}


def wait_cid(timeout: int = 20) -> str:
    """A new chat URL can lag a few seconds behind send confirmation."""
    started = time.time()
    while time.time() - started < timeout:
        cid = current_cid()
        if cid:
            return str(cid)
        time.sleep(2)
    return ""


def save_submitted_metadata(
    artifact_dir: Path,
    photo: Path,
    image_path: Path,
    card: dict[str, str],
    conversation_url: str,
    before: dict,
    submitted_at: str,
    pipeline_status: str,
    after: dict | None = None,
) -> None:
    metadata = {
        "version": "v10.0",
        "runner_version": "v10-pipeline-1",
        "workflow": "visual-decision-card -> queued-direct-render -> revisit-download",
        "submitted_at": submitted_at,
        "photo": str(photo.resolve()),
        "image": str(image_path.resolve()),
        "conversation_url": conversation_url,
        "pipeline_status": pipeline_status,
        "card": card,
        "state_before_render": before,
    }
    if after is not None:
        metadata["downloaded_at"] = datetime.now().astimezone().isoformat()
        metadata["state_after_render"] = after
    write_text(artifact_dir / "06-metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))


def process_one(
    photo: Path,
    image_path: Path,
    artifact_dir: Path,
    *,
    start_new_conversation: bool = True,
) -> bool:
    if image_path.exists() and image_path.stat().st_size > 0:
        print(f"  ↪ 已存在，跳过: {image_path}", flush=True)
        return True

    artifact_dir.mkdir(parents=True, exist_ok=True)
    write_text(artifact_dir / "01-card-instruction.txt", CARD_TEMPLATE)

    if start_new_conversation:
        nav = post("/navigate", {"url": "https://chatgpt.com/"})
        if isinstance(nav, dict) and nav.get("err"):
            print(f"  ❌ navigate 失败: {nav}", flush=True)
            return False
        if not wait_new_conversation(timeout=45):
            print("  ❌ 新会话未就绪", flush=True)
            return False
    if not upload_photo(str(photo)):
        print("  ❌ 照片上传失败", flush=True)
        return False

    if not send_msg(CARD_TEMPLATE, allow_refresh=False, refresh_hook=make_photo_hook(str(photo))):
        print("  ❌ 视觉卡发送失败", flush=True)
        return False
    if not wait_design(0, timeout=180):
        print("  ❌ 视觉卡超时", flush=True)
        return False

    card_text = (last_assistant() or "").strip()
    card = parse_card(card_text)
    issues = card_issues(card)
    if issues:
        print(f"  ⚠️ 视觉卡校验: {'; '.join(issues)}，请求原地修正一次", flush=True)
        fix = (
            "请修正刚才的视觉决策卡。仍然只输出规定字段、每行一个字段，不要解释。"
            "缺失字段必须补全，ROUTE 必须使用给定枚举，CANVAS_DIRECTION 必须明确横版、竖版或方形。"
        )
        if send_msg(fix) and wait_design(len(card_text), timeout=120):
            card_text = (last_assistant() or "").strip()
            card = parse_card(card_text)
            issues = card_issues(card)
    write_text(artifact_dir / "02-visual-decision-card.txt", card_text)
    if issues:
        write_text(artifact_dir / "card-validation-errors.txt", "\n".join(issues))
        print(f"  ❌ 视觉卡仍无效: {'; '.join(issues)}", flush=True)
        return False

    print(
        f"  🧭 {card.get('ROUTE')} | {card.get('CANVAS_DIRECTION')} | "
        f"主形: {card.get('HERO_TRANSFORMATION', '')[:70]}",
        flush=True,
    )

    render_instruction = build_render_instruction(card_text)
    write_text(artifact_dir / "03-render-instruction.txt", render_instruction)
    before = state()
    prev_gen = before.get("gen", 0)
    if not send_msg(render_instruction, allow_refresh=False, refresh_hook=make_photo_hook(str(photo))):
        print("  ❌ 直接出图指令发送失败", flush=True)
        return False
    print("  ✓ 直接出图指令已发，等待生成…", flush=True)
    if not wait_image(prev_gen, timeout=360):
        write_text(artifact_dir / "04-assistant-response.txt", last_assistant() or "")
        print("  ❌ 出图超时", flush=True)
        return False

    response = last_assistant() or ""
    conversation_url = str(eval_js("location.href") or "")
    write_text(artifact_dir / "04-assistant-response.txt", response)
    write_text(artifact_dir / "05-conversation-url.txt", conversation_url)
    metadata = {
        "version": "v10.0",
        "workflow": "visual-decision-card -> direct-render",
        "generated_at": datetime.now().astimezone().isoformat(),
        "photo": str(photo.resolve()),
        "image": str(image_path.resolve()),
        "conversation_url": conversation_url,
        "card": card,
        "state_before_render": before,
        "state_after_render": state(),
    }
    write_text(artifact_dir / "06-metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    ok = do_download(image_path, prev_gen)
    print(f"  {'✅' if ok else '❌'} 下载: {image_path}", flush=True)
    return bool(ok)


def submit_one(photo: Path, image_path: Path, artifact_dir: Path, index: int) -> dict | None:
    """Prepare one visual card and submit render, returning immediately after acceptance."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    write_text(artifact_dir / "01-card-instruction.txt", CARD_TEMPLATE)

    nav = post("/navigate", {"url": "https://chatgpt.com/"})
    if isinstance(nav, dict) and nav.get("err"):
        print(f"  ❌ navigate 失败: {nav}", flush=True)
        return None
    if not wait_new_conversation(timeout=45):
        print("  ❌ 新会话未就绪", flush=True)
        return None
    if not upload_photo(str(photo)):
        print("  ❌ 照片上传失败", flush=True)
        return None

    if not send_msg(CARD_TEMPLATE, allow_refresh=False, refresh_hook=make_photo_hook(str(photo))):
        print("  ❌ 视觉卡发送失败", flush=True)
        return None
    if not wait_design(0, timeout=180):
        print("  ❌ 视觉卡超时", flush=True)
        return None

    card_text = (last_assistant() or "").strip()
    card = parse_card(card_text)
    issues = card_issues(card)
    if issues:
        print(f"  ⚠️ 视觉卡校验: {'; '.join(issues)}，请求原地修正一次", flush=True)
        fix = (
            "请修正刚才的视觉决策卡。仍然只输出规定字段、每行一个字段，不要解释。"
            "缺失字段必须补全，ROUTE 必须使用给定枚举，CANVAS_DIRECTION 必须明确横版、竖版或方形。"
        )
        if send_msg(fix) and wait_design(len(card_text), timeout=120):
            card_text = (last_assistant() or "").strip()
            card = parse_card(card_text)
            issues = card_issues(card)
    write_text(artifact_dir / "02-visual-decision-card.txt", card_text)
    if issues:
        write_text(artifact_dir / "card-validation-errors.txt", "\n".join(issues))
        print(f"  ❌ 视觉卡仍无效: {'; '.join(issues)}", flush=True)
        return None

    print(
        f"  🧭 {card.get('ROUTE')} | {card.get('CANVAS_DIRECTION')} | "
        f"主形: {card.get('HERO_TRANSFORMATION', '')[:70]}",
        flush=True,
    )
    render_instruction = build_render_instruction(card_text)
    write_text(artifact_dir / "03-render-instruction.txt", render_instruction)
    before = state()
    prev_gen = before.get("gen", 0)
    if not send_msg(render_instruction, allow_refresh=False, refresh_hook=make_photo_hook(str(photo))):
        print("  ❌ 直接出图指令发送失败", flush=True)
        return None

    cid = wait_cid()
    if not cid:
        print("  ❌ 出图指令已发但未取得对话 CID，不能安全回访", flush=True)
        return None
    conversation_url = f"https://chatgpt.com/c/{cid}"
    submitted_at = datetime.now().astimezone().isoformat()
    write_text(artifact_dir / "05-conversation-url.txt", conversation_url)
    save_submitted_metadata(
        artifact_dir, photo, image_path, card, conversation_url, before,
        submitted_at, "submitted",
    )
    print(f"  🚀 已投递 {cid[:8]}…，不等图片完成，立即推进下一张", flush=True)
    return {
        "index": index,
        "name": photo.stem,
        "photo": str(photo.resolve()),
        "image": str(image_path.resolve()),
        "artifact_dir": str(artifact_dir.resolve()),
        "cid": cid,
        "conversation_url": conversation_url,
        "prev_gen": prev_gen,
        "submitted_at": submitted_at,
        "status": "submitted",
    }


def process_pipeline(photos: list[Path], out_dir: Path, suffix: str) -> tuple[int, int]:
    """Submit all render jobs first, then revisit from job 1 and download.

    The browser remains single-tab: this overlaps server-side image generation
    without simultaneous streaming tabs.  State is saved after every submit
    and download, so rerunning the same command resumes instead of resubmitting.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / ".v10-pipeline-manifest.json"
    manifest = read_manifest(manifest_path)
    jobs: list[dict] = manifest["jobs"]
    known_photos = {str(Path(job["photo"]).resolve()) for job in jobs}

    print("===== Phase A：视觉卡 + 投递出图（只确认受理，不等生成） =====", flush=True)
    for index, raw_photo in enumerate(photos, 1):
        photo = raw_photo.resolve()
        stem = f"{index:02d}-{photo.stem}{suffix}"
        image_path = out_dir / f"{stem}.png"
        artifact_dir = out_dir / f"{stem}-artifacts"
        photo_key = str(photo)
        if image_path.exists() and image_path.stat().st_size > 0:
            manifest["failures"] = [
                item for item in manifest["failures"] if item.get("photo") != photo_key
            ]
            print(f"  ↪ [{index:02d}] 已有成品，跳过投递: {image_path.name}", flush=True)
            continue
        if photo_key in known_photos:
            print(f"  ↪ [{index:02d}] 已在 manifest，跳过重复投递: {photo.name}", flush=True)
            continue

        print(f"===== [投递 {index:02d}/{len(photos)}] {photo.name} =====", flush=True)
        # A previous submit failure is retryable; replace its stale marker.
        manifest["failures"] = [
            item for item in manifest["failures"] if item.get("photo") != photo_key
        ]
        try:
            job = submit_one(photo, image_path, artifact_dir, index)
        except Exception as exc:
            job = None
            print(f"  ❌ 投递异常: {exc}", flush=True)
        if job is None:
            manifest["failures"].append({
                "index": index,
                "photo": photo_key,
                "stage": "submit",
                "at": datetime.now().astimezone().isoformat(),
            })
        else:
            jobs.append(job)
            known_photos.add(photo_key)
        write_json(manifest_path, manifest)
        if index < len(photos):
            time.sleep(2)

    manifest["phase"] = "download"
    write_json(manifest_path, manifest)
    waiting = [job for job in jobs if job.get("status") != "downloaded"]
    print(f"===== Phase A 完成：{len(waiting)} 个会话已投递；Phase B 从第 1 张回访下载 =====", flush=True)

    ok = 0
    for position, job in enumerate(sorted(waiting, key=lambda value: value["index"]), 1):
        image_path = Path(job["image"])
        artifact_dir = Path(job["artifact_dir"])
        if image_path.exists() and image_path.stat().st_size > 0:
            job["status"] = "downloaded"
            ok += 1
            write_json(manifest_path, manifest)
            continue

        print(f"===== [下载 {position}/{len(waiting)}] {job['name']} ({job['cid'][:8]}…) =====", flush=True)
        nav = post("/navigate", {"url": job["conversation_url"]})
        if isinstance(nav, dict) and nav.get("err"):
            print(f"  ❌ 回访失败: {nav}", flush=True)
            job["status"] = "download_failed"
            continue
        time.sleep(5)
        if not wait_image(job["prev_gen"], timeout=360):
            write_text(artifact_dir / "04-assistant-response.txt", last_assistant() or "")
            job["status"] = "generation_failed"
            print("  ❌ 回访后仍无成品图", flush=True)
            write_json(manifest_path, manifest)
            continue

        response = last_assistant() or ""
        write_text(artifact_dir / "04-assistant-response.txt", response)
        if do_download(image_path, job["prev_gen"]):
            ok += 1
            job["status"] = "downloaded"
            card = parse_card((artifact_dir / "02-visual-decision-card.txt").read_text(encoding="utf-8"))
            before = json.loads((artifact_dir / "06-metadata.json").read_text(encoding="utf-8")).get(
                "state_before_render", {}
            )
            save_submitted_metadata(
                artifact_dir,
                Path(job["photo"]),
                image_path,
                card,
                job["conversation_url"],
                before,
                job["submitted_at"],
                "downloaded",
                state(),
            )
        else:
            job["status"] = "download_failed"
        write_json(manifest_path, manifest)

    failures = [job for job in jobs if job.get("status") != "downloaded"]
    submit_failures = manifest.get("failures", [])
    manifest["phase"] = "complete" if not failures and not submit_failures else "incomplete"
    write_json(manifest_path, manifest)
    ok = sum(
        1
        for index, photo in enumerate(photos, 1)
        if (out_dir / f"{index:02d}-{photo.resolve().stem}{suffix}.png").exists()
    )
    fail_count = len(failures) + len(submit_failures)
    print(f"完成：✅ {ok} 张 | ❌ {fail_count} 张失败", flush=True)
    return ok, fail_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("photos", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--suffix", default="_拾景纸刊v10")
    parser.add_argument(
        "--serial",
        action="store_true",
        help="保守串行模式：每张等生成和下载后再开始下一张；默认使用后台生成流水线",
    )
    parser.add_argument(
        "--single-conversation",
        action="store_true",
        help="实验模式：所有照片串行使用同一个 ChatGPT 对话",
    )
    args = parser.parse_args()

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(post("/connect", {"port": 9224}), flush=True)

    if args.single_conversation:
        print("===== single-conversation 实验模式 =====", flush=True)
        nav = post("/navigate", {"url": "https://chatgpt.com/"})
        if isinstance(nav, dict) and nav.get("err"):
            print(f"  ❌ navigate 失败: {nav}", flush=True)
            raise SystemExit(1)
        if not wait_new_conversation(timeout=45):
            print("  ❌ 新会话未就绪", flush=True)
            raise SystemExit(1)
        ok = fail = 0
        for index, photo in enumerate(args.photos, 1):
            photo = photo.resolve()
            stem = f"{index:02d}-{photo.stem}{args.suffix}"
            image_path = out_dir / f"{stem}.png"
            artifact_dir = out_dir / f"{stem}-artifacts"
            print(f"===== [同一对话 {index}/{len(args.photos)}] {photo.name} =====", flush=True)
            try:
                success = process_one(
                    photo,
                    image_path,
                    artifact_dir,
                    start_new_conversation=False,
                )
            except Exception as exc:
                success = False
                print(f"  ❌ 异常: {exc}", flush=True)
            if success:
                ok += 1
            else:
                fail += 1
            if index < len(args.photos):
                time.sleep(5)
        print(f"完成：✅ {ok} 张 | ❌ {fail} 张失败", flush=True)
    elif not args.serial:
        ok, fail = process_pipeline(args.photos, out_dir, args.suffix)
    else:
        print("===== serial 回退模式 =====", flush=True)
        ok = fail = 0
        for index, photo in enumerate(args.photos, 1):
            photo = photo.resolve()
            stem = f"{index:02d}-{photo.stem}{args.suffix}"
            image_path = out_dir / f"{stem}.png"
            artifact_dir = out_dir / f"{stem}-artifacts"
            print(f"===== [{photo.name}] =====", flush=True)
            try:
                success = process_one(photo, image_path, artifact_dir)
            except Exception as exc:
                success = False
                print(f"  ❌ 异常: {exc}", flush=True)
            if success:
                ok += 1
            else:
                fail += 1
            if index < len(args.photos):
                time.sleep(5)

    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    main()
