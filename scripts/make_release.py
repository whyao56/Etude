"""把打好的包发成 GitHub Release —— 一条命令发版。

为什么需要脚本而不是手动点网页：发布有 4 步（打包 → 算校验和 → 建 Release →
传附件），手点容易漏掉校验和，或者把一个旧包传上去。脚本把
「包和校验和必须同源」变成默认行为。

为什么 exe 走 Release 而不是提交进仓库：
  1. 二进制产物不该进版本历史（仓库会膨胀，而且每次改代码都多一份副本，
     历史再也瘦不回去）；
  2. GitHub 单文件硬限制 100 MB，而解压后的目录有好几百个文件。
  Release 附件就是官方为此设计的出口：可下载、有稳定 URL、可校验、不污染历史。

凭据从 git 的凭据管理器读（``git credential fill``），**不落盘、不进环境变量**。

用法：
    python scripts/make_release.py --check      # 只打包、打印校验和，不上传
    python scripts/make_release.py --dry-run    # 再加一步：走一遍 API 但不建 release
    python scripts/make_release.py              # 真发版
    python scripts/make_release.py --repo owner/name --tag v0.0.1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import products  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"

DEFAULT_REPO = "whyao56/Etude"

# 压缩包里的使用说明。新手最容易踩的坑写在最前面。
README_IN_ZIP = """Etude · 句子五通道语言训练器 {version}
================================================

怎么用
------
1. 把整个文件夹解压到任意位置（桌面、D 盘都行，路径有中文也没关系）
2. 双击 Etude.exe
3. 第一次打开先去「设置」里填大模型（选厂商 + 填 API Key），
   然后到「训练包」页输入一个词或一句话，生成第一个训练包

【重要】不要只把 Etude.exe 单独拖出来运行。
    它旁边的 _internal 文件夹是程序的一部分，少一个文件都起不来。
    要挪位置就整个文件夹一起挪。

起不来怎么办
------------
在命令行里跑一次自检，它会直接告诉你缺什么：
    Etude.exe --check
结果同时写在数据目录的 logs\\selfcheck.txt（见下）。

数据存在哪
----------
训练包、复习记录、设置、语音缓存都在：
    C:\\Users\\<你的用户名>\\AppData\\Local\\Etude
卸载 = 删掉程序文件夹 + 删掉上面这个目录。
换个位置解压程序，数据不会丢。

关于联网
--------
生成新训练包要联网（调大模型 + 合成语音）。
已经生成好的包里，例句、语音、复习进度都是本机的，断网也能练。

开源地址：https://github.com/{repo}
"""


def version() -> str:
    text = (ROOT / "backend" / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("读不出版本号。")
    return match.group(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def token() -> str:
    """从 git 凭据管理器取。不落盘、不打印。"""
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"取不到 git 凭据：{exc}") from exc
    if out.returncode != 0:
        raise SystemExit("git credential fill 失败了，先确认本机能 push 到 GitHub。")
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise SystemExit("git 凭据里没有密码/token。")


def call(method: str, url: str, tok: str, data: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"message": payload[:400]}


def make_zip(tag_version: str, repo: str, dist: Path | None = None) -> Path:
    """把打包产物打成一个 zip，内含使用说明。"""
    dist = dist or products.newest_output()
    if dist is None or not dist.is_dir():
        raise SystemExit(products.describe_dist() + "\n先跑 scripts/build_exe.py。")

    zip_path = ROOT / "dist" / f"Etude-{tag_version}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()

    note = README_IN_ZIP.format(version=tag_version, repo=repo)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(f"Etude-{tag_version}/使用说明.txt", note)
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                arc = Path(f"Etude-{tag_version}") / path.relative_to(dist)
                zf.write(path, str(arc).replace("\\", "/"))
    return zip_path


def find_release(tok: str, repo: str, tag: str) -> dict | None:
    status, data = call("GET", f"{API}/repos/{repo}/releases/tags/{tag}", tok)
    return data if status == 200 else None


def upload_asset(tok: str, repo: str, release_id: int, path: Path) -> dict:
    url = (
        f"{UPLOAD}/repos/{repo}/releases/{release_id}/assets"
        f"?name={urllib.parse.quote(path.name)}"
    )
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST")
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("Content-Type", "application/zip")
    try:
        with urllib.request.urlopen(request, timeout=900) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"上传失败（HTTP {exc.code}）：{body[:400]}") from exc


def verify_download(url: str, expect_bytes: int) -> tuple[bool, str]:
    """下载页实测：真的能 200 拉回来，而且大小对得上。"""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            if size and size != expect_bytes:
                return False, f"大小对不上：页面 {size}，本地 {expect_bytes}"
            return True, f"HTTP 200，{human(size or expect_bytes)}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"连不上：{exc.reason}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("ETUDE_REPO", DEFAULT_REPO))
    parser.add_argument("--version", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--notes", default=None, help="发行说明文件")
    parser.add_argument("--check", action="store_true", help="只打包和算校验和")
    parser.add_argument("--dry-run", action="store_true", help="走一遍但不真发")
    parser.add_argument("--dist", default=None,
                        help="产物目录（默认取最近一次打包的那份，见 scripts/products.py）")
    args = parser.parse_args()

    ver = args.version or version()
    tag = args.tag or f"v{ver}"
    notes_path = Path(args.notes) if args.notes else ROOT / "docs" / "releases" / f"{tag}.md"

    dist = Path(args.dist).resolve() if args.dist else products.newest_output()

    print(f"[1/6] 打包 zip（{ver}）")
    zip_path = make_zip(ver, args.repo, dist)
    digest = sha256(zip_path)
    print(f"      源目录 {dist}")
    print(f"      {zip_path.name} · {human(zip_path.stat().st_size)}")
    print(f"      SHA256 {digest}")

    if args.check:
        print("\n--check 到此为止，没有上传。")
        return 0

    if not notes_path.is_file():
        raise SystemExit(f"找不到发行说明 {notes_path}，先写一份再发。")
    notes = notes_path.read_text(encoding="utf-8")

    # 校验和要印在发行说明里。如果不一致，说明说明是旧版本留下的 ——
    # 那正是「文档和产物对不上」的经典症状，宁可拦住也不要发出去。
    if digest not in notes:
        print(f"\n⚠ 发行说明里没有这个包的 SHA256。")
        print(f"  请把下面这一行补进 {notes_path.name} 的「文件校验」一节：")
        print(f"  {zip_path.name}  SHA256: {digest}")
        if not args.dry_run:
            raise SystemExit("发行说明和产物对不上，先改说明再发。")

    print(f"[2/6] 取发布凭据")
    tok = token()
    print("      已从 git 凭据管理器取到（不落盘、不打印）")

    print(f"[3/6] 检查远端状态（{args.repo}）")
    status, repo_info = call("GET", f"{API}/repos/{args.repo}", tok)
    if status != 200:
        raise SystemExit(
            f"读不到仓库 {args.repo}（HTTP {status}）：{repo_info.get('message')}\n"
            "确认仓库已创建、且 token 有 repo 权限。"
        )
    print(f"      仓库存在：{repo_info.get('full_name')}（默认分支 {repo_info.get('default_branch')}）")

    existing = find_release(tok, args.repo, tag)
    if existing:
        print(f"      已存在同名 release {tag}（id {existing['id']}），"
              "将复用它并覆盖同名附件。")

    if args.dry_run:
        print("\n--dry-run 到此为止，没有建 release、没有上传。")
        return 0

    print(f"[4/6] 打 tag 并推送")
    if not _git_tag(tag):
        raise SystemExit(f"打 tag 失败。")

    print(f"[5/6] 建 release {tag}")
    if existing:
        release_id = existing["id"]
        call("PATCH", f"{API}/repos/{args.repo}/releases/{release_id}", tok,
             {"body": notes, "draft": False})
    else:
        status, created = call("POST", f"{API}/repos/{args.repo}/releases", tok, {
            "tag_name": tag,
            "name": f"Etude {ver}",
            "body": notes,
            "draft": False,
            "prerelease": ver.startswith("0.0."),
        })
        if status not in (200, 201):
            raise SystemExit(f"建 release 失败（HTTP {status}）：{created.get('message')}")
        release_id = created["id"]
    print(f"      release id {release_id}")

    print("[6/6] 上传附件并验证下载页")
    asset = upload_asset(tok, args.repo, release_id, zip_path)
    print(f"      已上传：{asset.get('name')}（{human(asset.get('size', 0))}）")

    url = asset.get("browser_download_url", "")
    ok, detail = verify_download(url, zip_path.stat().st_size)
    print(f"      下载页实测：{'OK  ' if ok else '失败'} {detail}")
    if not ok:
        print("      ⚠ 下载页没通。附件可能还在处理，过一分钟再手动试一次。")

    print(f"\n发布完成：https://github.com/{args.repo}/releases/tag/{tag}")
    print(f"sha256（用来核对下载）：{digest}")
    return 0 if ok else 1


def _git_tag(tag: str) -> bool:
    """打 tag 并推送。已存在就跳过。"""
    have = subprocess.run(["git", "tag", "--list", tag], cwd=str(ROOT),
                          text=True, capture_output=True).stdout.strip()
    if not have:
        result = subprocess.run(["git", "tag", "-a", tag, "-m", f"Etude {tag}"],
                                cwd=str(ROOT), text=True, capture_output=True)
        if result.returncode != 0:
            print("      " + (result.stderr or "").strip())
            return False
        print(f"      已新建 tag {tag}")
    else:
        print(f"      tag {tag} 已存在")

    result = subprocess.run(["git", "push", "origin", tag], cwd=str(ROOT),
                            text=True, capture_output=True, timeout=180)
    if result.returncode != 0:
        print("      推送 tag 失败：" + (result.stderr or result.stdout or "").strip()[:300])
        return False
    print(f"      已推送 tag {tag}")
    return True


if __name__ == "__main__":
    sys.exit(main())
