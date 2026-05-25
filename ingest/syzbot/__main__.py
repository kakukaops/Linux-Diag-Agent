"""Entry point: python -m ingest.syzbot incremental"""
import sys, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from ingest.syzbot.ingester import SyzbotIngester
ingester = SyzbotIngester()
report = ingester.run()
sys.exit(0 if report.status == "ok" else 1)
