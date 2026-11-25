"""Explicit replacement of validated DWG outputs with mandatory verified backups."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _version(path: Path) -> bytes:
    with path.open('rb') as stream:
        return stream.read(6)


def replace_with_backup(original: Path, candidate: Path, expected_sha256: str) -> dict:
    original, candidate = Path(original).resolve(), Path(candidate).resolve()
    if original == candidate:
        raise ValueError('成品与原图路径相同，禁止覆盖')
    if any(original.with_suffix(ext).exists() for ext in ('.dwl', '.dwl2')):
        raise ValueError('原图存在 CAD 锁文件；请关闭图纸并确认锁状态后重试')
    if sha256(original) != expected_sha256:
        raise ValueError('原图在处理期间发生变化，禁止覆盖')
    version = _version(original)
    if len(version) != 6 or not version.startswith(b'AC') or not version[2:].isdigit() or _version(candidate) != version:
        raise ValueError('成品与原图 DWG 版本不一致，禁止覆盖')
    candidate_hash = sha256(candidate)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_fd, backup_name = tempfile.mkstemp(prefix=original.name+'.backup-'+timestamp+'-', suffix='.bak', dir=original.parent)
    os.close(backup_fd)
    backup = Path(backup_name)
    # Keep even a failed/partial backup for diagnosis; never overwrite an older backup.
    shutil.copy2(original, backup)
    if sha256(backup) != expected_sha256:
        raise ValueError(f'备份校验失败，禁止覆盖：{backup}')
    pending_fd, pending_name = tempfile.mkstemp(prefix='.'+original.name+'.replace-', suffix='.tmp', dir=original.parent)
    os.close(pending_fd)
    pending = Path(pending_name)
    try:
        shutil.copy2(candidate, pending)
        if sha256(pending) != candidate_hash or _version(pending) != version:
            raise ValueError('替换副本校验失败，禁止覆盖')
        if sha256(original) != expected_sha256:
            raise ValueError('原图在备份期间发生变化，禁止覆盖')
        if any(original.with_suffix(ext).exists() for ext in ('.dwl', '.dwl2')):
            raise ValueError('替换前发现 CAD 锁文件，禁止覆盖')
        os.replace(pending, original)
    except (OSError, ValueError) as exc:
        raise type(exc)(f'{exc}；原图未由本次替换修改，已保留备份：{backup}') from exc
    finally:
        pending.unlink(missing_ok=True)
    return {'backup_path': str(backup), 'backup_sha256': expected_sha256,
            'original_path': str(original), 'output_sha256': candidate_hash,
            'dwg_version': version.decode('ascii'), 'replaced': True}


def overwrite_result(result: dict, expected_sha256: str) -> None:
    """Apply replacement only after the owning workflow has verified its output."""
    if result.get('fatal') or result.get('skipped'):
        return
    try:
        if not result.get('output_dwg'):
            raise ValueError('没有验收成品，禁止覆盖')
        if 'verify' in result:
            if result.get('verify') != 'print' or not result.get('clean'):
                raise ValueError('清戳未通过前后打印验收，禁止覆盖')
        elif not result.get('after'):
            raise ValueError('修复成品未完成复验，禁止覆盖')
        if not result.get('dwg_version_before') or result.get('dwg_version_before') != result.get('dwg_version_after'):
            raise ValueError('处理前后版本未验收一致，禁止覆盖')
        result['overwrite'] = replace_with_backup(Path(result['input']), Path(result['output_dwg']), expected_sha256)
    except (OSError, ValueError) as exc:
        result['fatal'] = f'备份/覆盖失败：{exc}'


def overwrite_group(records, cache):
    """Replace dependencies before their host; shared inputs get one backup per run."""
    if any(r.get('fatal') for r in records):
        return False
    for result in records:
        if result.get('skipped'):
            continue
        source = str(Path(result['input']).resolve())
        if source in cache:
            if sha256(Path(source)) != cache[source]['output_sha256']:
                result['fatal'] = '已处理参照在本次任务中再次变化，禁止重复覆盖'
                return False
            result['overwrite'] = cache[source]
            continue
        expected = result.get('source_sha256')
        if not expected:
            result['fatal'] = '缺少处理前原件指纹，禁止覆盖'
            return False
        overwrite_result(result, expected)
        if result.get('fatal'):
            return False
        if result.get('overwrite'):
            cache[source] = result['overwrite']
    return True
