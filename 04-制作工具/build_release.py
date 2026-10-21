"""Create a relocatable artwork-and-tool release; never include old runtime trees."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "04-制作工具"
sys.path.insert(0, str(TOOLS / "production"))

CORE = {
    "deck.py", "library.py", "runtime_paths.py", "runtime_state.py",
    "merchant_export.py", "import_candidate.py", "selection_publish.py",
}
UI = {"index.html", "app.js", "style.css", "production.js", "production.css",
      "library.js", "library.css", "serve_review.py"}
DOCS = {"工具说明.md", "图像生成工作流.md", "layout-example.json",
        "requirements.txt", "启动选图.ps1", "build_release.py"}


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def safe_files(folder):
    for file in sorted(folder.rglob("*")):
        if file.is_symlink() or getattr(file, "is_junction", lambda: False)():
            raise ValueError("发行来源不能依赖目录连接：" + str(file))
        if file.is_file() and not any(part in {"__pycache__", ".preview-cache"} for part in file.parts):
            if file.suffix not in {".log", ".lock", ".tmp", ".pyc"}:
                yield file


def zip_release(target):
    target = Path(target).resolve()
    if not target.is_relative_to(ROOT / "01-发布成品/工具发行") or not (target / "发行清单.json").is_file():
        raise ValueError("只能打包工具发行目录内已有清单的完整版本。")
    manifest = json.loads((target / "发行清单.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        file = target / entry["path"]
        if not file.resolve().is_relative_to(target) or sha(file) != entry["sha256"]:
            raise ValueError("发行文件与清单不一致：" + entry["path"])
    archive_path = target.with_name(target.name + ".zip")
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
        for file in safe_files(target):
            archive.write(file, (Path(target.name) / file.relative_to(target)).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("发行ZIP校验失败。")
    return {"zip": str(archive_path), "sha256": sha(archive_path), "zip_bytes": archive_path.stat().st_size}


def build(destination=None, make_zip=True):
    from runtime_state import file_lock
    state = ROOT / "05-制作记录/当前状态"
    if not all((state / file).is_file() for file in ("selections.json", "deck-config.json")):
        raise ValueError("请先通过启动选图建立当前状态。")
    final_target = (Path(destination) if destination else ROOT / "01-发布成品/工具发行" /
              ("ai-girls-poker-studio-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if not final_target.is_relative_to(ROOT) or final_target.exists():
        raise ValueError("发行目标须为项目内尚不存在的独立目录。")
    target = final_target.with_name("." + final_target.name + ".building")
    if target.exists():
        raise ValueError("存在未完成构建目录，请使用新的发行目录名。")
    target.mkdir(parents=True)
    files = [ROOT / "README.md", ROOT / "启动选图.cmd"]
    files += [TOOLS / name for name in sorted(DOCS)]
    files += [TOOLS / "webui" / name for name in sorted(UI)]
    files += [TOOLS / "production" / name for name in sorted(CORE)]
    files += list((TOOLS / "production").glob("test_*.py"))
    files += list(safe_files(TOOLS / "resources"))
    files += list(safe_files(ROOT / "02-创作资产"))
    files += list(safe_files(ROOT / "03-原始素材"))
    # Historical audit snapshots are provenance, not runtime configuration.
    files = [p for p in files if p.name not in {"registry-before-consolidation.json", "catalog-before-consolidation.json"}]
    copied = []
    for source in files:
        relative = source.relative_to(ROOT)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        copied.append(relative)
    with file_lock(state / ".writer.lock"):
        for name in ("selections.json", "deck-config.json"):
            relative = Path("05-制作记录/当前状态") / name
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes((state / name).read_bytes())
            copied.append(relative)
    release_intro = target / "发行说明.txt"
    release_intro.write_text(
        "AI娘扑克工坊 · 素材与制作工具\n"
        "先安装 Python 3.10+ 与 04-制作工具/requirements.txt，再双击启动选图.cmd。\n"
        "全部创作候选在02-创作资产；选择仅为状态，不删除其他版本。\n"
        "保存选择自动输出原画；制作页生成54正面＋1统一背＋6盒面的PNG和CMYK JPG。\n"
        "图像生成使用Codex或你自选的绘图环境，然后导入本工具。\n"
        "此包不含旧商家成品、旧输出与运行缓存；可从携带的选图状态重新导出。\n"
        "Windows已验收；其他系统可使用Python服务入口，但未作实机验收。\n",
        encoding="utf-8-sig",
    )
    (target / "01-发布成品").mkdir()
    (target / "01-发布成品/说明.txt").write_text(
        "启动后选择原画输出到当前选择；点击生成后输出到导出批次。旧包不会被覆盖。\n", encoding="utf-8-sig")
    registry = json.loads((target / "02-创作资产/asset-registry.json").read_text(encoding="utf-8-sig"))
    for asset in registry["assets"].values():
        for key in ("art_source", "preview", "name_asset"):
            if asset.get(key):
                path = Path(asset[key])
                if path.is_absolute() or not (target / path).resolve().is_relative_to(target) or not (target / path).is_file():
                    raise ValueError(f"发行包素材引用未闭合：{asset.get('id')} {key} {path}")
    for key, value in registry["name_assets"].items():
        if Path(value).is_absolute() or not (target / value).is_file():
            raise ValueError("名称素材缺失：" + key)
    for relative in copied:
        file = target / relative
        if file.suffix.lower() in {".py", ".ps1", ".json", ".md", ".txt", ".js", ".html", ".csv"}:
            # Block inadvertent personal paths, rather than silently damaging source records.
            body = file.read_text(encoding="utf-8-sig", errors="replace")
            normalized = body.replace("\\\\", "\\").replace("\\", "/").casefold()
            if any(str(marker).replace("\\", "/").casefold() in normalized for marker in (ROOT, Path.home())):
                raise ValueError("发行资料仍含本机个人路径：" + str(relative))
    entries = [{"path": p.relative_to(target).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)}
               for p in safe_files(target)]
    selections = json.loads((target / "05-制作记录/当前状态/selections.json").read_text(encoding="utf-8-sig"))
    manifest = {"schema_version": 1, "created_at": datetime.now().astimezone().isoformat(),
                "asset_count": len(registry["assets"]), "selection_revision": selections["revision"],
                "selected_count": len(selections["selected_ids"]), "files": entries}
    (target / "发行清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.rename(final_target)
    target = final_target
    result = {"directory": str(target), "files": len(entries) + 1, "bytes": sum(e["bytes"] for e in entries)}
    if make_zip:
        result.update(zip_release(target))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", help="New directory inside the project")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--zip-existing", help="Archive an already built and verified release directory")
    args = parser.parse_args()
    if args.zip_existing:
        print(json.dumps(zip_release(args.zip_existing), ensure_ascii=False, indent=2), flush=True)
    else:
        build(args.destination, not args.no_zip)
