#!/usr/bin/env python3
"""Quick GPU benchmark — measures raw keys/s for each GPU device."""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.resolve()))

import pyopencl as cl

KERNEL_SRC = (Path(__file__).parent / "gpu_engine" / "gpu_kernel.h").read_text(
    encoding="utf-8",
)


def benchmark_device(
    platform_idx: int,
    device_idx: int,
    batch_size: int,
    label: str,
    n_runs: int = 3,
) -> float:
    """Run benchmark on a single GPU device and return average keys/s."""
    platforms = cl.get_platforms()
    pf = platforms[platform_idx]
    devices = pf.get_devices()
    dev = devices[device_idx]

    ctx = cl.Context([dev])
    queue = cl.CommandQueue(ctx, dev)

    # Determine build options based on vendor
    vendor = dev.vendor.lower()
    if "nvidia" in vendor:
        build_opts = ["-cl-std=CL1.2", "-cl-mad-enable", "-cl-fast-relaxed-math"]
        local_ws = 128
    elif "intel" in vendor:
        # CL3.0's strict address space qualifier checking fails on Arc;
        # use CL1.2 (works fine, same compute throughput without ARC_OPT)
        build_opts = ["-cl-std=CL1.2", "-cl-mad-enable"]
        local_ws = 64
    else:
        build_opts = ["-cl-std=CL1.2"]
        local_ws = 64

    # Build kernel with vendor-optimized options
    prog = cl.Program(ctx, KERNEL_SRC).build(options=build_opts)
    kernel_hash160 = prog.ec_mul_hash160

    # Allocate buffers with COPY_HOST_PTR (USE_HOST_PTR fails on some drivers)
    h_privkeys = np.zeros(batch_size * 32, dtype=np.uint8)
    h_hash160s = np.zeros(batch_size * 20, dtype=np.uint8)

    mf = cl.mem_flags
    d_privkeys = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=h_privkeys)
    d_hash160s = cl.Buffer(ctx, mf.WRITE_ONLY, size=batch_size * 20)

    rates = []
    for run in range(n_runs):
        # Fill with random private keys (use os.urandom for real randomness)
        rand = np.frombuffer(np.random.bytes(batch_size * 32), dtype=np.uint8).copy()
        h_privkeys[:] = rand

        # Copy to device
        cl.enqueue_copy(queue, d_privkeys, h_privkeys).wait()

        t0 = time.perf_counter()

        # Ensure global_size is multiple of local_ws
        gws = ((batch_size + local_ws - 1) // local_ws) * local_ws

        kernel_hash160.set_args(d_privkeys, d_hash160s, np.uint32(batch_size))
        cl.enqueue_nd_range_kernel(queue, kernel_hash160, (gws,), (local_ws,)).wait()
        cl.enqueue_copy(queue, h_hash160s, d_hash160s).wait()

        elapsed = time.perf_counter() - t0
        rate = batch_size / elapsed
        rates.append(rate)
        print(
            f"  {label} Run {run + 1}: {batch_size:,} keys in {elapsed:.3f}s = {rate:,.0f} keys/s",
        )

    d_privkeys.release()
    d_hash160s.release()
    # Context may not support release() on all platforms
    return sum(rates) / len(rates)


def main() -> None:
    platforms = cl.get_platforms()
    devices_info = []
    for pi, p in enumerate(platforms):
        for di, d in enumerate(p.get_devices()):
            if d.type == cl.device_type.GPU:
                devices_info.append(
                    (
                        pi,
                        di,
                        d.name.strip(),
                        d.max_compute_units,
                    ),
                )

    print(f"Found {len(devices_info)} GPU device(s):")
    for pi, di, name, cu in devices_info:
        print(f"  Platform[{pi}] Device[{di}]: {name} ({cu} CU)")

    print("\n--- Benchmark Results ---")
    results = []
    for pi, di, name, cu in devices_info:
        batch = 131072 if "Arc" in name else 65536
        print(f"\nBenchmarking: {name}")
        avg = benchmark_device(pi, di, batch, name)
        results.append((name, avg))
        print(f"  => Average: {avg:,.0f} keys/s")

    print("\n=== Summary ===")
    for name, rate in results:
        print(f"  {name}: {rate:,.0f} keys/s")


if __name__ == "__main__":
    main()
