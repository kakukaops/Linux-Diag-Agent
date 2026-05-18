"""Entry point: python -m ingest.bugzilla incremental"""
import sys, logging
logging.basicConfig(level=logging.INFO)
from ingest.bugzilla.ingester import BugzillaIngester
ingester = BugzillaIngester()
report = ingester.run()
sys.exit(0 if report.status == "ok" else 1)
