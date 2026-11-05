import asyncio
import logging
import sys

from .config import Config
from .controller import reconcile, run
from .controller import kconfig


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    if "--once" in sys.argv:  # run a single pass, e.g. with OUTPUT_PATH=./config.yml for local testing
        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        asyncio.run(reconcile(cfg))
    else:
        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
