"""Quota/TTL collection limited to completed jobs' reproducible .work files."""
import os
import stat
import time
from pathlib import Path
from core.storage import JobBusyError, cache_source_pin
from core.storage_policy import cleanup_forbidden


class CacheManager:
    def __init__(self, store, output_root):
        self.store = store
        self.root = Path(output_root).resolve()

    @staticmethod
    def _path_key(path):
        return os.path.normcase(str(Path(path).resolve()))

    def _safe(self, path):
        path = Path(os.path.abspath(path))
        try:
            if os.path.commonpath([str(path.resolve()), str(self.root)]) != str(self.root):
                return False
            # Junctions/reparse points and symlinks are never traversed.
            for component in [path, *path.parents]:
                if component == self.root.parent: break
                info = component.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                    return False
            relative = path.relative_to(self.root)
            return len(relative.parts) >= 2 and '.work' in relative.parts[1:]
        except (OSError,ValueError):
            return False

    def _jobs(self):
        offset = 0
        while True:
            batch = self.store.list_jobs(1000, offset)
            yield from batch
            if len(batch) < 1000: break
            offset += len(batch)

    def collect(self, quota_bytes=20*1024**3, ttl_days=30, dry_run=False, protected_paths=()):
        # Serialize collectors for this output root, including independent DBs.
        with self.store.project_lock(str(self.root)):
            return self._collect(quota_bytes,ttl_days,dry_run,protected_paths)

    def _collect(self, quota_bytes, ttl_days, dry_run, protected_paths):
        if quota_bytes is not None and quota_bytes <= 0:
            raise ValueError('缓存容量必须为正数或None')
        if ttl_days is not None and ttl_days < 0:
            raise ValueError('保留天数不能为负数')
        entries = self.store.list_cache()
        used = {}
        def walk_values(value, stamp):
            if isinstance(value, dict):
                for v in value.values(): walk_values(v, stamp)
            elif isinstance(value, list):
                for v in value: walk_values(v, stamp)
            elif isinstance(value, str) and os.path.isabs(value):
                used[os.path.normcase(os.path.abspath(value))] = max(used.get(os.path.normcase(os.path.abspath(value)),0), stamp)
        from datetime import datetime
        for entry in entries:
            try: timestamp = datetime.fromisoformat(entry['last_used_at']).timestamp()
            except (ValueError,TypeError): timestamp = time.time()
            walk_values(entry['outputs'], timestamp)
        jobs = list(self._jobs())
        for job in jobs:
            for stage in self.store.list_stages(job['id']):
                if stage.get('last_used_at'):
                    try:stamp=datetime.fromisoformat(stage['last_used_at']).timestamp()
                    except (ValueError,TypeError):continue
                    walk_values(stage['outputs'],stamp)
        protected = {self._path_key(p) for p in protected_paths if p}
        # Even DONE projects may use another project's .work file as original input.
        protected.update(self._path_key(j['source_path']) for j in jobs if j.get('source_path'))
        files = []; total = 0; pinned = 0; errors = []
        for job in jobs:
            project = job.get('project_dir')
            if not project: continue
            work = Path(project)/'.work'
            if not work.is_dir(): continue
            # Check before walking, then reject each linked directory.
            if not self._safe(work): continue
            for directory, subdirs, names in os.walk(work, followlinks=False):
                subdirs[:] = [d for d in subdirs if self._safe(Path(directory)/d)]
                for name in names:
                    path = Path(directory)/name
                    if not self._safe(path): continue
                    try:
                        info = path.stat(); total += info.st_size
                        if job['status'] != 'DONE' or job.get('owner_token') or self._path_key(path) in protected:
                            pinned += info.st_size; continue
                        stamp = max(info.st_mtime,used.get(os.path.normcase(str(path.absolute())),0))
                        files.append((stamp,job['id'],path,info.st_size,info.st_mtime_ns))
                    except OSError as exc: errors.append(str(exc))
        target = int(quota_bytes*.8) if quota_bytes is not None and total > quota_bytes else total
        cutoff = time.time()-ttl_days*86400 if ttl_days is not None else None
        selected = []; freed = 0
        for stamp, job_id, path, size, mtime_ns in sorted(files):
            if total-freed <= target and (cutoff is None or stamp >= cutoff): continue
            try:
                with self.store.job_lock(job_id), self.store.transaction() as conn:
                    job = self.store.get_job(job_id)
                    if not job or job['status'] != 'DONE' or job.get('owner_token'): continue
                    if self._path_key(path) in protected: continue
                    pin = cache_source_pin(path)
                    if pin is not None and os.path.lexists(str(pin)): continue
                    # Refresh references while holding the SQLite writer lock.
                    # create_job cannot publish a new reference between this
                    # check and unlink; the initial scan is only an LRU snapshot.
                    source_paths = conn.execute('SELECT source_path FROM jobs').fetchall()
                    if any(self._path_key(row[0]) == self._path_key(path) for row in source_paths):
                        continue
                    if cleanup_forbidden(path): continue
                    if not self._safe(path): continue
                    current = path.stat()
                    if (current.st_size,current.st_mtime_ns) != (size,mtime_ns): continue
                    if not dry_run:
                        path.unlink()
                        self.store.invalidate_paths([str(path)])
                    selected.append(str(path)); freed += size
            except (OSError,JobBusyError,ValueError) as exc:
                errors.append(str(exc))
        return dict(dry_run=dry_run,total_bytes=total,pinned_bytes=pinned,reclaimed_bytes=freed,
                    remaining_bytes=total-freed,selected_paths=selected,errors=errors,
                    quota_met=quota_bytes is None or total-freed <= quota_bytes)
