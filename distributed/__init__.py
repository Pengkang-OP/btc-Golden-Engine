"""分布式扫描包 - Master-Worker 架构.

基于 gRPC 的分布式私钥碰撞扫描系统.

使用示例:
  # 启动 Master(在本机,默认端口 50051)
  python -m distributed.master --port 50051

  # 启动 Worker 并连接到 Master
  python -m distributed.worker --master-addr localhost:50051 --worker-id node-1
"""

from distributed.master import MasterService, WorkerRegistry
from distributed.models import Assignment, WorkerInfo
from distributed.worker import DistributedScanner

__all__ = [
    "Assignment",
    "DistributedScanner",
    "MasterService",
    "WorkerInfo",
    "WorkerRegistry",
]
