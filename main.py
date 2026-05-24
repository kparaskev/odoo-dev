import argparse,json
from odoo_dev.config import load_config, inject_odoo_paths
from odoo_dev.rpc import OdooRPC
from odoo_dev.analyzer import OdooAnalyzer


def main() -> None:
    parser = argparse.ArgumentParser(description="Odoo dev toolkit")
    parser.add_argument(
        "--force", action="store_true",
        help="Rebuild module caches even if they already exist",
    )
    args = parser.parse_args()

    config = load_config()
    inject_odoo_paths(config)

    rpc = OdooRPC(config)
    print(f"Server  : {config.url}")
    print(f"Version : {rpc.server_version()}")
    print(f"UID     : {rpc.uid}\n")

    analyzer = OdooAnalyzer(config, rpc, force=args.force)

    print(f"\nRemote installed : {len(analyzer.remote_modules)}")
    print(f"Local available  : {len(analyzer.local_modules)}")
    print(f"  odoo       : {sum(1 for m in analyzer.local_modules if m['source'] == 'odoo')}")
    print(f"  enterprise : {sum(1 for m in analyzer.local_modules if m['source'] == 'enterprise')}")
    print(f"  custom     : {sum(1 for m in analyzer.local_modules if m['source'] == 'custom')}")
    print(f"\nInstalled + local source : {len(analyzer.installed_and_local())}")

    missing = analyzer.installed_but_not_local()
    print(f"Installed but no local source : {len(missing)}")
    if missing:
        print("  " + ", ".join(missing[:10]) + ("…" if len(missing) > 10 else ""))

    result = analyzer.find_model_implementations("sale.order")
    print(json.dumps(result, indent=4))
    
if __name__ == "__main__":
    main()
