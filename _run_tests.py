import subprocess, sys, os

cmd = [
    sys.executable, "-m", "pytest", "tests/", "distributed/test_distributed.py",
    "-v", "--tb=line", "-x",
    "--ignore=tests/test_benchmark_regression.py",
    "--ignore=tests/test_e2e_collision.py",
    "-q"
]
result = subprocess.run(cmd, capture_output=True, text=True)
out = result.stdout or ""
err = result.stderr or ""
with open("pytest_full_out.txt", "w", encoding="utf-8") as f:
    f.write(out)
    f.write("\nSTDERR:\n")
    f.write(err)
print(f"Exit code: {result.returncode}")
# Show last 20 lines
lines = out.split("\n")
for l in lines[-20:]:
    print(l)
sys.exit(result.returncode)
