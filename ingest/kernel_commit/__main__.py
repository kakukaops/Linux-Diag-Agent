"""Entry point: python -m ingest.kernel_commit incremental"""
import sys, logging
logging.basicConfig(level=logging.INFO)
from ingest.kernel_commit.ingester import KernelCommitIngester
ingester = KernelCommitIngester()
report = ingester.run()
sys.exit(0 if report.status == "ok" else 1)
