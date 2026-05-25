"""Entry point: python -m ingest.nvd incremental"""
import sys, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from ingest.nvd.ingester import NvdIngester
ingester = NvdIngester()
report = ingester.run()
sys.exit(0 if report.status == "ok" else 1)
