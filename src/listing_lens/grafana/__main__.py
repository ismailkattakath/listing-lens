"""python -m listing_lens.grafana [apply | --check | build UID]"""

from .apply import cli

if __name__ == "__main__":
    cli()
