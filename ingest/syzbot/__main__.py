"""Entry point: python -m ingest.syzbot [incremental] [--max N]

  --max N  Override _MAX_PER_RUN for this invocation. Default is the
           class constant (2000 in v2.3). Use --max 6000 to cover all
           fixed-dashboard crashes in one shot (~2.5h at 1.5s/request).
"""
import sys, logging, argparse
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ap = argparse.ArgumentParser()
ap.add_argument("mode", nargs="?", default="incremental",
                choices=("incremental",))
ap.add_argument("--max", dest="max_per_run", type=int, default=None,
                help="override SyzbotIngester._MAX_PER_RUN for this run")
args = ap.parse_args()

from ingest.syzbot.ingester import SyzbotIngester
if args.max_per_run is not None:
    SyzbotIngester._MAX_PER_RUN = args.max_per_run
    logging.info("syzbot _MAX_PER_RUN overridden → %d", args.max_per_run)

ingester = SyzbotIngester()
report = ingester.run()
sys.exit(0 if report.status == "ok" else 1)
