import asyncio
import logging
import sys

from .config import Config
from .controller import load_kube, reconcile, run


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    load_kube()
    # --once: a single pass, e.g. with OUTPUT_PATH=./config.yml for local testing
    asyncio.run(reconcile(cfg) if "--once" in sys.argv else run(cfg))


if __name__ == "__main__":
    main()
