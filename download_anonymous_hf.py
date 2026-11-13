#!/usr/bin/env python3
"""逐文件下载 Anonymous HF 资源，仅使用 Python 3 标准库。"""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def safe_target(root, remote_path):
    path = PurePosixPath(remote_path)
    if not remote_path or path.is_absolute() or ".." in path.parts or "\\" in remote_path:
        raise ValueError(f"目录接口返回不安全的路径: {remote_path!r}")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"路径超出下载目录: {remote_path!r}")
    return target


def verify_file(path, entry):
    if not path.is_file():
        return False
    expected_size = entry.get("size")
    if expected_size is not None and path.stat().st_size != int(expected_size):
        return False
    lfs = entry.get("lfs") or {}
    digest = lfs.get("oid") or lfs.get("sha256")
    if digest:
        digest = digest.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"无法识别文件 SHA256: {entry['path']}")
        hasher = hashlib.sha256()
    elif re.fullmatch(r"[0-9a-fA-F]{40}", entry.get("oid", "")):
        digest = entry["oid"]
        hasher = hashlib.sha1()
        hasher.update(f"blob {path.stat().st_size}\0".encode())
    else:
        # 没有大小和校验值时，不把现有文件视为已验证的下载结果。
        return expected_size is not None
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower() == digest.lower()


def retry(operation, label, attempts):
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as error:
            print(f"[失败 {attempt}/{attempts}] {label}: {error}", flush=True)
            if attempt == attempts:
                raise
            time.sleep(min(attempt * 3, 15))


def iter_files(opener, base, timeout, attempts, root, cache_root, refresh, failed_directories):
    """逐目录返回文件，目录失败后继续，成功的目录列表保存到本地。"""
    pending = [""]
    visited = set()
    seen_files = set()
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        print(f"[目录] {directory or '/'}", flush=True)
        url = base + "tree/" + urllib.parse.quote(directory, safe="/")
        cache = cache_root / (hashlib.sha256(directory.encode()).hexdigest() + ".json")

        def validate(tree):
            if not isinstance(tree, list):
                raise ValueError("目录接口没有返回文件列表")
            for entry in tree:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ValueError("目录条目缺少有效的 path 字段")
                safe_target(root, entry["path"])
                if entry.get("type") not in ("file", "directory"):
                    raise ValueError(f"无法识别条目类型: {entry}")
            return tree

        def get_tree():
            with opener.open(url, timeout=timeout) as response:
                return validate(json.load(response))

        try:
            entries = None
            if cache.is_file() and not refresh:
                try:
                    entries = validate(json.loads(cache.read_text(encoding="utf-8")))
                    print("  使用已保存的目录列表", flush=True)
                except (OSError, ValueError):
                    print("  目录缓存不可用，重新读取", flush=True)
            if entries is None:
                entries = retry(get_tree, f"读取目录 {directory or '/'}", attempts)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_root,
                                                 delete=False) as output:
                    temporary = Path(output.name)
                    try:
                        json.dump(entries, output, ensure_ascii=False)
                        output.close()
                        os.replace(temporary, cache)
                    finally:
                        temporary.unlink(missing_ok=True)
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as error:
            failed_directories.append(directory or "/")
            print(f"[目录未完成] {directory or '/'}: {error}，继续处理其他目录", flush=True)
            continue
        children = []
        for entry in entries:
            path = entry["path"]
            if entry["type"] == "directory":
                children.append(path)
            elif path not in seen_files:
                seen_files.add(path)
                yield entry
        # 优先获取模型权重，避免大量物体网格目录延迟测试所需文件的下载。
        children.sort(key=lambda path: (path.split("/")[0] != "check_points", path))
        pending.extend(reversed(children))


def download_file(opener, base, entry, root, timeout):
    target = safe_target(root, entry["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".anonymous-hf-part")
    if partial.is_symlink():
        raise ValueError(f"临时下载路径是符号链接: {partial}")
    url = base + "resolve/" + urllib.parse.quote(entry["path"], safe="/")
    received = 0
    last_report = time.monotonic()
    with opener.open(url, timeout=timeout) as response, partial.open("wb") as output:
        header_size = response.headers.get("Content-Length")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            if time.monotonic() - last_report >= 15:
                print(f"  {entry['path']}: {received / 1048576:.1f} MiB", flush=True)
                last_report = time.monotonic()
    if header_size is not None and received != int(header_size):
        raise ValueError(f"响应不完整，预期 {header_size} 字节，收到 {received}")
    # 有大小或哈希时验证，避免把错误页或中断的数据当成成功文件。
    has_metadata = entry.get("size") is not None or entry.get("lfs") or entry.get("oid")
    if has_metadata and not verify_file(partial, entry):
        raise ValueError("文件大小或哈希校验失败")
    if not has_metadata and received == 0:
        raise ValueError("缺少校验信息，且文件内容为空")
    os.replace(partial, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", default="nlm8uiuqaius", help="匿名资源 ID")
    parser.add_argument("--output", default="anonymous_repo", help="下载到独立目录")
    parser.add_argument("--proxy", default="", help="例如 http://127.0.0.1:7890，省略则直连")
    parser.add_argument("--attempts", type=int, default=5, help="每个请求最多尝试次数")
    parser.add_argument("--timeout", type=float, default=90, help="单次网络操作超时秒数")
    parser.add_argument("--refresh-tree", action="store_true", help="重新获取目录信息并更新目录缓存")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.id) or args.attempts < 1 or args.timeout <= 0:
        parser.error("资源 ID、尝试次数或超时参数无效")
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache_root = safe_target(root, f".anonymous-hf-cache/{args.id}")
    cache_root.mkdir(parents=True, exist_ok=True)
    # 使用显式代理设置，避开原环境中 socks:// 等错误配置。
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    opener.addheaders = [("User-Agent", "AnonymousHF-file-downloader/1.0")]
    base = f"https://anonymous-hf.com/api/a/{args.id}/"
    print(f"保存至 {root}，边读取目录边下载文件", flush=True)
    failed_directories = []
    entries = iter_files(opener, base, args.timeout, args.attempts, root, cache_root,
                         args.refresh_tree, failed_directories)
    failed = []
    total = downloaded = skipped = 0
    for index, entry in enumerate(entries, 1):
        total = index
        try:
            target = safe_target(root, entry["path"])
            if verify_file(target, entry):
                skipped += 1
                print(f"[跳过 {index}] 已校验 {entry['path']}", flush=True)
                continue
            print(f"[下载 {index}] {entry['path']}", flush=True)
            retry(lambda: download_file(opener, base, entry, root, args.timeout),
                  entry["path"], args.attempts)
            downloaded += 1
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as error:
            failed.append(entry["path"])
            print(f"[未完成] {entry['path']}: {error}", flush=True)
    print(f"\n本轮发现 {total} 个文件，下载完成 {downloaded} 个，校验后跳过 {skipped} 个。")
    if failed_directories:
        print("以下目录未能列出，其中的文件尚未全部确认，重新运行可重试:")
        print("\n".join(failed_directories))
    if failed:
        print("\n以下文件未完成，重新运行相同命令可重试:")
        print("\n".join(failed))
    if failed or failed_directories:
        return 1
    if total == 0:
        print("目录接口未返回文件，请确认资源内容。", file=sys.stderr)
        return 1
    print(f"\n接口列出的 {total} 个文件全部下载完成。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中止，重新运行可跳过校验通过的完整文件。", file=sys.stderr)
        sys.exit(130)
