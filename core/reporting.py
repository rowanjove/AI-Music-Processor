"""Project rights inventory from explicit evidence records, never from DSP success."""
import os


def dependency_evidence(project):
    """Human-readable evidence from this job's frozen records, not live imports."""
    versions, identities = {}, {}
    for stage in project.get('stage_records', []):
        parameters=stage.get('parameters') or {}
        name=stage.get('stage_name','unknown')
        for package,version in (parameters.get('package_versions') or {}).items():
            versions.setdefault((str(package),str(version)),set()).add(name)
        for artifact,digest in (parameters.get('dependency_hashes') or {}).items():
            identities.setdefault((str(artifact),str(digest)),set()).add(name)
    def escape(value):
        return value.replace('|','\\|').replace('\r',' ').replace('\n',' ')
    rows=['\n## 本任务依赖证据\n',
          '以下来自本任务冻结的 Stage 记录；未知版本不推定为已确认。SHA-256 识别实际依赖，不代表音质或授权通过。\n',
          '| 依赖 | 冻结版本 | Stage |','|---|---|---|']
    for (package,version),stages in sorted(versions.items()):
        rows.append('| {} | {} | {} |'.format(escape(package),escape(version),', '.join(sorted(stages))))
    if not versions:rows.append('| 未记录 | 未确认 | 不补造当前环境版本 |')
    rows.extend(['\n| 依赖文件/组件 | SHA-256 | Stage |','|---|---|---|'])
    for (artifact,digest),stages in sorted(identities.items()):
        rows.append('| {} | {} | {} |'.format(escape(artifact),escape(digest),', '.join(sorted(stages))))
    if not identities:rows.append('| 未记录 | 未确认 | 不补造当前环境指纹 |')
    return '\n'.join(rows)+'\n'


def license_manifest(store, mode, model_key, model_name, source_path, source_hash):
    def record(kind, key):
        rows = store.list_licenses(kind,key)
        if len(rows) == 1 and not rows[0].get('conflict'):
            return rows[0]
        return dict(subject_type=kind,subject_id=key,commercial_status='Unknown',note='未记录完整授权证据或存在冲突')
    source = record('input',source_hash)
    required = [source]
    model = record('model',model_key) if mode == 'voice_conversion' else None
    voice = record('voice',model_key) if mode == 'voice_conversion' else None
    if model: required += [model,voice]
    states = [r.get('commercial_status','Unknown') for r in required]
    overall = 'Ready' if all(s == 'Ready' for s in states) else ('Warning' if 'Warning' in states else 'Unknown')
    return dict(commercial_status=overall,
                rvc_model=dict(id=model_key,name=model_name,**{k:model.get(k) for k in ('commercial_status','license_name','source_url','author','note')}) if model else None,
                voice_rights=voice,input_rights=source,samples=[],
                input_source=os.path.basename(source_path),source_sha256=source_hash,
                notes='状态仅基于用户记录的证据完整性；处理成功不代表取得声音、模型或输入授权。未参与项目的样本档案不自动关联。')


def build_candidate_comparison_summary(candidates, winner_id=None, selection_reason=None, reference_stem_available=False, user_reviewed=False):
    """Build honest candidate comparison data without inventing scores or SDR without references."""
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise ValueError("候选列表不能为空")

    rows = []
    for cand in candidates:
        cid = cand.get("candidate_id") or cand.get("id") or "unknown"
        stage_records = cand.get("stage_records") or []
        metrics = cand.get("metrics") or {}

        sdr = metrics.get("sdr")
        if not reference_stem_available or sdr is None:
            sdr_status = "NOT_MEASURABLE (无干净参考音轨)"
            sdr_val = None
        else:
            sdr_status = "MEASURED"
            sdr_val = float(sdr)

        rvc_metrics = cand.get("rvc_metrics") or {}
        f0_validity = rvc_metrics.get("f0_validity_ratio")
        clipped_samples = rvc_metrics.get("clipped_samples", 0)
        residual_ms = rvc_metrics.get("residual_ms")

        rows.append({
            "candidate_id": cid,
            "stages_executed": [s.get("stage_name") for s in stage_records if isinstance(s, dict)],
            "separation_sdr_status": sdr_status,
            "separation_sdr": sdr_val,
            "f0_validity_ratio": f0_validity,
            "clipped_samples": clipped_samples,
            "residual_sync_ms": residual_ms,
            "model_identity": cand.get("model_identity", "unknown"),
            "params_snapshot": cand.get("parameters", {}),
        })

    is_fair = len({c.get("source_id") for c in candidates if c.get("source_id")}) <= 1

    return {
        "schema_version": 1,
        "candidates_count": len(candidates),
        "fair_comparison": is_fair,
        "reference_stem_available": bool(reference_stem_available),
        "selected_winner_id": winner_id,
        "selection_reason": selection_reason or ("未提供自动选优理由" if winner_id else "未选择候选"),
        "user_reviewed": bool(user_reviewed),
        "listening_quality": "assessed" if user_reviewed else "not_assessed",
        "quality_acceptance": "READY_FOR_EXTERNAL_VALIDATION",
        "candidate_rows": rows,
    }


def format_candidate_comparison_report(summary):
    """Render candidate comparison summary as Markdown."""
    lines = [
        "## 候选链路质量对比报告 (Candidate Comparison)",
        "",
        "- 公平比对条件成立：%s" % ("是（同源输入）" if summary.get("fair_comparison") else "否（可能跨素材）"),
        "- 干净参考音轨：%s" % ("具备（SDR 可测量）" if summary.get("reference_stem_available") else "缺失（SDR 标记为 NOT_MEASURABLE）"),
        "- 选用候选：%s" % (summary.get("selected_winner_id") or "无"),
        "- 选用理由：%s" % summary.get("selection_reason", ""),
        "- 人工试听确认：%s" % ("是" if summary.get("user_reviewed") else "否（听审状态保持 not_assessed）"),
        "- 验收状态：%s" % summary.get("quality_acceptance", "READY_FOR_EXTERNAL_VALIDATION"),
        "",
        "| 候选 ID | 执行阶段 | 分离 SDR/SI-SDR | 音高有效率 | 削波点数 | 残余对齐 (ms) | 模型身份 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in summary.get("candidate_rows", []):
        sdr_str = str(row["separation_sdr"]) if row["separation_sdr"] is not None else row["separation_sdr_status"]
        f0_str = ("%.1f%%" % (row["f0_validity_ratio"] * 100)) if row["f0_validity_ratio"] is not None else "未评估"
        res_str = ("%.2fms" % row["residual_sync_ms"]) if row["residual_sync_ms"] is not None else "未记录"
        stages_str = ", ".join(row["stages_executed"]) or "无"
        lines.append(f"| {row['candidate_id']} | {stages_str} | {sdr_str} | {f0_str} | {row['clipped_samples']} | {res_str} | {row['model_identity']} |")

    return "\n".join(lines) + "\n"
