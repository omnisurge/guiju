"""文件指纹与原子写入，以及下载目录的幂等查找。

pull / show 共用的本地文件语义都在这一层：文件名怎么取（naming）、
落盘后的「已有」怎么判（find_cached）、默认下载到哪（default_out_dir）。
"""

from __future__ import annotations

import glob
import hashlib
import os
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_out_dir() -> Path:
    """下载目录默认值：~/.guiju/library（纯文件缓存，不是库）。"""
    from .store.sqlite import default_home  # 延迟导入：files 不反向依赖 store

    return default_home() / "library"


def find_cached(
    out_dir: Path, code_key: str | None, doc_name: str, *, readable: bool = False
) -> tuple[Path, str] | None:
    """下载目录里的幂等判断：按可复现的文件名找，不查库。

    默认命名带 sha256 指纹（__{sha前12}.pdf）：逐个算一遍摘要，
    指纹和文件名对得上才算「已有」，坏文件/被改过的文件会自动漏过去重下。
    --readable 没有指纹，文件在就算已有，不校验内容（使用者自行负责）。

    同名主干视为同一文件：code_key 为空时按 doc_name 主干匹配，
    与命名层的截断行为一致——是取舍不是缺陷，同一份正文按两种入口
    （公告ID / 名称）重复拉取时应命中同一落盘文件。
    """
    from .domain import naming

    if readable:
        path = out_dir / naming.readable_name(code_key, doc_name)
        if path.is_file():
            return path, sha256_file(path)
        return None
    pattern = glob.escape(naming.library_stem(code_key, doc_name)) + "__*.pdf"
    for path in sorted(out_dir.glob(pattern)):
        digest = sha256_file(path)
        if path.name == naming.library_name(code_key, doc_name, digest):
            return path, digest
    return None


def write_atomic(path: str | Path, data: bytes) -> Path:
    """先写临时文件再改名：中途失败不会留下半个 PDF 让你误判成功。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target
