# Applying the v0.3 patch

This is a SURGICAL patch — it contains only new/changed code files. It does
NOT touch your theses/, memory.db, .env, or config/ files. Your data and
edits are safe.

## Apply (from your repo root, in Git Bash or PowerShell)

    cd "C:\Users\stanw\Dropbox\PC (2)\Desktop\stan-trading-tools\ai-quant-researcher"

Then extract this zip on top (Python method, reliable on Windows):

    python -c "
    import zipfile, shutil, os
    with zipfile.ZipFile('stan_fork_overlay_v0.3_patch.zip') as z:
        z.extractall('_tmp_patch/')
    src = '_tmp_patch/stan_fork_overlay_v0.3_patch'
    for item in os.listdir(src):
        s = os.path.join(src, item)
        if os.path.isdir(s):
            shutil.copytree(s, item, dirs_exist_ok=True)
        else:
            shutil.copy2(s, item)
    shutil.rmtree('_tmp_patch')
    print('v0.3 patch applied.')
    "

## Verify

    set PYTHONIOENCODING=utf-8      (PowerShell: $env:PYTHONIOENCODING='utf-8')
    python scripts/verify_install.py

Expect 16 passed, 0 failed (1 Deribit test may WARN if network blocked).

## Then re-check your accepted strategy through the new gate

    python -m quant_validator.vs_random run --thesis_id skew_consensus_v21
    python -m quant_validator.gates evaluate --thesis_id skew_consensus_v21

See PATCH_NOTES_v0.3.md for full detail.
