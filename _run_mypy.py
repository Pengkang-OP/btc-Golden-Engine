"""Run mypy like CI does."""
import subprocess, sys

cmd = [
    sys.executable, "-m", "mypy",
    "__main__.py", "collision_engine.py", "collision_target.py",
    "extract_utxo_hash160.py", "extract_utxo_xonly.py",
    "core/", "api/", "gpu_engine/", "distributed/", "tests/",
    "--ignore-missing-imports", "--platform", "linux"
]
result = subprocess.run(cmd, capture_output=True, text=True)
out = (result.stdout or "") + (result.stderr or "")
with open("mypy_out.txt", "w", encoding="utf-8") as f:
    f.write(out)
# Print summary
lines = out.split("\n")
for l in lines:
    if " error:" in l or "Found " in l or "Success" in l:
        print(l)
print(f"Exit code: {result.returncode}")
