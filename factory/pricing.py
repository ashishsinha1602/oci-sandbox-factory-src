#!/usr/bin/env python3
"""Live OCI pricing, and what an AWS stack costs once it moves.

Prices come from Oracle's public price list - no account, no key:

    https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/

    python pricing.py                      # what a sandbox costs
    python pricing.py --gb 1000            # for a terabyte of data
    python pricing.py --share 10           # a cluster split across 10 workloads
    python pricing.py --migrate glue,lambda,iceberg

Two things this is careful about, because both mislead people:

  * A shared cluster is charged once, not once per workload. Ten jobs on one
    Data Flow pool each carry a tenth of it. `--share` divides accordingly.
  * Serverless pieces cost nothing while idle. A Function or a Queue that is
    provisioned but unused is genuinely free, unlike a provisioned cluster.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

PRICE_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode={cur}"
HOURS_PER_MONTH = 744

# What to look for in Oracle's price list. First match wins, so order matters.
SKUS = {
    # Names as Oracle publishes them, confirmed against the live list. Several
    # services appear twice - once at 0 for the Always Free allowance and once
    # at the real rate - so build_prices() prefers the priced row.
    "a1_ocpu":       ["Compute - Standard - A1 - OCPU"],          # published as a free-tier row only
    "a1_memory":     ["Compute - Standard - A1 - Memory"],
    "e4_ocpu":       ["Compute - Standard - E4 - OCPU"],
    "e4_memory":     ["Compute - Standard - E4  - Memory", "Compute - Standard - E4 - Memory"],
    # sandbox databases are Autonomous Transaction Processing, LICENSE_INCLUDED:
    # not the BYOL or Vector Database rows (a quarter of the price)
    "adb_ecpu":      ["Oracle Autonomous AI Transaction Processing - ECPU"],
    "adb_storage":   ["Oracle Autonomous AI Database Storage for Transaction Processing"],
    "object_gb":     ["Object Storage - Storage"],
    "object_req":    ["Object Storage - Requests"],
    "fn_time":       ["Oracle Functions - Execution Time"],
    "fn_calls":      ["Oracle Functions - Invocations"],
    "queue_req":     ["OCI Queue"],
    "kafka_ocpu":    ["OCI Streaming With Apache Kafka"],
    "nosql_write":   ["Oracle NoSQL Database Cloud - Write"],
    "nosql_read":    ["Oracle NoSQL Database Cloud - Read"],
    "nosql_storage": ["Oracle NoSQL Database Cloud - Storage"],
    "gateway_calls": ["API Gateway"],
    "egress_gb":     ["Outbound Data Transfer - Originating in North America"],
    "di_workspace":  ["OCI Data Integration - Workspace Usage"],
    "di_data_gb":    ["OCI Data Integration - Data Processed"],
    "di_pipeline":   ["OCI Data Integration - Pipeline Operator Execution"],
}

# Published AWS on-demand list prices, us-east-1, for the same unit. Kept here
# rather than fetched: AWS has no equivalent open price endpoint, and a stale
# number quietly compared against a live one is worse than a labelled constant.
AWS = {
    "a1_ocpu":     (0.0408, "vCPU/hour (m7g on-demand, per vCPU)"),
    "adb_ecpu":    (0.2400, "RDS Oracle SE2 r6i.large, per vCPU/hour"),
    "object_gb":   (0.0230, "S3 Standard, GB/month"),
    "fn_calls":    (0.2000, "Lambda, per 1M requests"),
    "fn_time":     (0.0000166667, "Lambda, per GB-second"),
    "queue_req":   (0.4000, "SQS Standard, per 1M requests"),
    "kafka_ocpu":  (0.0750, "MSK kafka.m5.large, per broker/hour"),
    "gateway_calls": (3.5000, "API Gateway REST, per 1M calls"),
    "egress_gb":   (0.0900, "Data transfer out, GB"),
    "spark_ocpu":  (0.4400, "Glue ETL, per DPU-hour"),
    "di_workspace": (0.4900, "MWAA mw1.small environment, per hour"),
}

# How an AWS stack lands on OCI. Two routes for each: lift the code as it is,
# or change it and use the service that fits OCI better.
MIGRATION = {
    "lambda": {
        "aws": "AWS Lambda",
        "same": ("OCI Functions", "A Lambda handler is a container here. Package the same code "
                 "with the fn runtime and the signature barely moves."),
        "better": ("OCI Functions behind API Gateway, or a container instance",
                   "If the function runs for minutes rather than seconds, or holds a database "
                   "connection, a small always-on container is cheaper and simpler than paying "
                   "per-invocation for something that never sleeps."),
    },
    "s3": {
        "aws": "Amazon S3",
        "same": ("OCI Object Storage", "S3-compatible API: point the SDK at the OCI endpoint and "
                 "change the credentials. Most code needs no change beyond the endpoint."),
        "better": ("Object Storage with a lifecycle policy", "Tiering and expiry are policy rather "
                   "than a Lambda on a schedule, so the cleanup code goes away."),
    },
    "glue": {
        "aws": "AWS Glue (ETL jobs)",
        "same": ("OCI Data Flow", "Managed Spark. A PySpark script runs unchanged; you register it "
                 "as an application pointing at the script in Object Storage."),
        "better": ("Data Flow + Data Catalog", "Glue's catalog maps to OCI Data Catalog, so jobs "
                   "resolve table names instead of hardcoded paths, same as Glue."),
    },
    "glue-catalog": {
        "aws": "AWS Glue Data Catalog",
        "same": ("OCI Data Catalog", "The metastore. Harvest a bucket or a database and jobs "
                 "resolve names against it."),
        "better": ("Data Catalog", "Same thing; there is no cheaper shortcut worth taking."),
    },
    "iceberg": {
        "aws": "Apache Iceberg on S3",
        "same": ("Iceberg on Object Storage via Data Flow",
                 "Iceberg is a table format, not a service. Spark on Data Flow reads and writes it "
                 "with the same iceberg runtime jar; only the warehouse path changes to oci://."),
        "better": ("Autonomous Database, if the data fits",
                   "Iceberg exists to give object storage ACID tables and time travel. An "
                   "Autonomous Database has those already, and below a few terabytes it is usually "
                   "less machinery for the same result."),
    },
    "athena": {
        "aws": "Amazon Athena",
        "same": ("Data Flow SQL", "Spark SQL over the same files in Object Storage."),
        "better": ("Autonomous Database external tables",
                   "Query object storage directly from SQL with no cluster at all."),
    },
    "airflow": {
        "aws": "Amazon MWAA (managed Airflow)",
        "same": ("Apache Airflow in a container on OCI",
                 "Oracle has no managed Airflow. Run the same Airflow image on a container instance "
                 "or OKE; DAGs move unchanged. About 8x cheaper than MWAA, and you do the patching."),
        "better": ("OCI Data Integration",
                   "Oracle's managed pipeline service - tasks, dependencies, schedules. Roughly 3x "
                   "cheaper than MWAA and nothing to operate, but DAGs are rebuilt as pipelines."),
    },
    "mwaa": {"aws": "Amazon MWAA", "same": ("Apache Airflow in a container on OCI",
             "Same image, same DAGs."), "better": ("OCI Data Integration", "Managed, but not Airflow.")},
    "step-functions": {"aws": "AWS Step Functions",
             "same": ("OCI Data Integration pipelines", "Visual orchestration of tasks."),
             "better": ("DBMS_SCHEDULER job chains", "When the steps are mostly SQL, chain them inside "
                        "the database with dependencies and retries and skip the extra service.")},
    "sqs":  {"aws": "Amazon SQS", "same": ("OCI Queue", "Same model: one consumer per message, "
             "visibility timeout, dead-letter after N failures."),
             "better": ("OCI Queue", "Already the right shape.")},
    "kinesis": {"aws": "Kinesis / MSK", "same": ("OCI Streaming", "Kafka-compatible endpoint, so "
                "existing Kafka clients connect unchanged."),
                "better": ("OCI Streaming", "Serverless pool rather than sized brokers.")},
    "dynamodb": {"aws": "DynamoDB", "same": ("OCI NoSQL", "Key-value and JSON documents, on-demand "
                 "or provisioned throughput."),
                 "better": ("OCI NoSQL", "Same shape.")},
    "rds":  {"aws": "RDS / Aurora", "same": ("Autonomous Database", "Managed Oracle, auto-scaling "
             "ECPUs."), "better": ("Autonomous Database", "Patching and tuning stop being your job.")},
    "eks":  {"aws": "Amazon EKS", "same": ("OKE", "Managed Kubernetes; manifests move unchanged."),
             "better": ("Container Instances", "If it is a handful of containers rather than a "
                        "platform, skip the cluster entirely.")},
}


def fetch(currency: str = "USD") -> list[dict]:
    with urllib.request.urlopen(PRICE_URL.format(cur=currency), timeout=90) as r:
        return json.loads(r.read())["items"]


def tiers(item: dict) -> list[tuple]:
    """Pay-as-you-go tiers as (price, range_min, range_max)."""
    out = []
    for loc in item.get("currencyCodeLocalizations") or []:
        for p in loc.get("prices") or []:
            if p.get("model") == "PAY_AS_YOU_GO":
                out.append((p.get("value"), p.get("rangeMin"), p.get("rangeMax")))
    return out


def payg(item: dict) -> float | None:
    """The paid rate. A tiered SKU (Functions, Queue) lists its free allowance
    first at 0; taking that first tier priced them as free forever."""
    vals = [t[0] for t in tiers(item) if t[0] is not None]
    return max(vals) if vals else None


def free_allowance(item: dict) -> float | None:
    """Units a month that cost nothing before the paid tier starts (in the
    SKU's own metric: 2 = 2 million invocations, 40 = 400,000 GB-seconds)."""
    for price, lo, hi in tiers(item):
        if price == 0 and hi and (lo or 0) == 0:
            return hi
    return None


def config_string(prices: dict) -> str:
    """The form SBX.FACTORY_CONFIG.oci_prices holds: 'key=price/metric; ...',
    plus 'key_free=N' for a monthly free allowance in the same metric."""
    parts = []
    for k, v in prices.items():
        if v.get("price") is None:
            continue
        parts.append(f"{k}={v['price']}/{v['metric']}")
        if v.get("free"):
            parts.append(f"{k}_free={v['free']}")
    return "; ".join(parts)


def build_prices(items: list[dict]) -> dict[str, dict]:
    """Resolve each SKU we care about to a live unit price.

    Oracle's list carries a zero-priced row for anything with an Always Free
    allowance, sitting right beside the row that says what it actually costs
    once you pass it. Taking the first match makes Object Storage look free and
    the whole comparison dishonest, so prefer a non-zero price and say plainly
    when the only published number was zero.
    """
    out: dict[str, dict] = {}
    for key, names in SKUS.items():
        candidates = []
        for want in names:
            for i in items:
                display = i.get("displayName") or ""
                if display.strip().lower() == want.lower() or (display.lower().startswith(want.lower()) and "byol" not in display.lower()):
                    candidates.append((display, payg(i), i.get("metricName", ""), free_allowance(i)))
        # An exact name beats a prefix: "NoSQL ... - Read" is the provisioned
        # rate the sandbox tables use; "- Read - Auto" (on-demand) is 25x that.
        exact = [c for c in candidates if c[0].strip().lower() in [n.lower() for n in names]]
        if [c for c in exact if c[1] not in (None, 0)]:
            candidates = exact
        priced = [c for c in candidates if c[1] not in (None, 0)]
        if priced:
            name, price, metric, free = max(priced, key=lambda c: c[1])
            out[key] = {"name": name, "price": price, "metric": metric, "free_tier": False, "free": free}
        elif candidates:
            name, _, metric, _ = candidates[0]
            out[key] = {"name": name, "price": 0.0, "metric": metric, "free_tier": True}
        else:
            out[key] = {"name": names[0], "price": None, "metric": "not in the price list",
                        "free_tier": False}
    return out


def money(v: float) -> str:
    if v == 0:
        return "free"
    if v < 0.01:
        return f"${v:,.4f}"
    return f"${v:,.2f}"


def rule(width: int = 78) -> str:
    return "-" * width


def show_catalogue(prices: dict, currency: str) -> None:
    print(f"\nLIVE OCI PRICES ({currency}, pay as you go)")
    print(rule())
    print(f"{'service':<26}{'unit price':>14}  {'per'}")
    print(rule())
    for key, p in prices.items():
        if p["price"] is None:
            val = "not listed"
        elif p.get("free_tier"):
            val = "free tier only"
        else:
            val = money(p["price"])
        print(f"{key:<26}{val:>14}  {p['metric'][:34]}")
    print(rule())
    print("Live from Oracle's published list. 'free tier only' means the list showed no paid rate")
    print("for that SKU - treat it as unpriced rather than free, and check the pricing page.")


def estimate(prices: dict, gb: float, share: int, hours: int) -> list[tuple]:
    """A month of a modest data pipeline: storage, a Spark run, a function, a queue."""
    def unit(key):
        p = prices.get(key, {}).get("price")
        return 0.0 if p is None else float(p)

    rows = []
    rows.append(("Object Storage", f"{gb:,.0f} GB stored", unit("object_gb") * gb))
    rows.append(("Object Storage requests", "1M requests", unit("object_req") * 100))
    spark_hours = hours
    spark_cost = (unit("e4_ocpu") * 4 + unit("e4_memory") * 64) * spark_hours
    rows.append((f"Data Flow Spark ({spark_hours}h)", "4 OCPU + 64 GB, shared",
                 spark_cost / max(share, 1)))
    rows.append(("Functions", "2M invocations", unit("fn_calls") * 2))
    rows.append(("Queue", "5M requests", unit("queue_req") * 5))
    rows.append(("API Gateway", "1M calls", unit("gateway_calls")))
    rows.append(("Egress", "100 GB out", unit("egress_gb") * 100))
    return rows


def show_estimate(rows: list[tuple], share: int, gb: float) -> None:
    print(f"\nMONTHLY ESTIMATE  -  {gb:,.0f} GB of data, cluster shared by {share} workload(s)")
    print(rule())
    print(f"{'line item':<34}{'basis':<24}{'cost':>18}")
    print(rule())
    for name, basis, cost in rows:
        print(f"{name:<34}{basis:<24}{money(cost):>18}")
    print(rule())
    total = sum(c for _, _, c in rows)
    print(f"{'TOTAL':<58}{money(total):>18}")
    if share > 1:
        print(f"\nThe Spark line is one cluster divided by {share}. Run it alone and that line is "
              f"{share}x larger; the serverless lines do not change, because they are charged per "
              f"use rather than per hour.")


def show_migration(which: list[str], prices: dict) -> None:
    print("\nMOVING FROM AWS")
    print(rule())
    for raw in which:
        key = raw.strip().lower()
        m = MIGRATION.get(key)
        if not m:
            print(f"\n{raw}: no mapping recorded. Known: {', '.join(sorted(MIGRATION))}")
            continue
        print(f"\n{m['aws']}")
        print(f"  1. same code      {m['same'][0]}")
        print(f"                    {m['same'][1]}")
        print(f"  2. changed code   {m['better'][0]}")
        print(f"                    {m['better'][1]}")
    print("\n" + rule())


def show_comparison(prices: dict) -> None:
    print("\nUNIT PRICE, OCI vs AWS")
    print(rule())
    print(f"{'unit':<18}{'OCI':>12}{'AWS':>12}{'':>4}{'what AWS charges for'}")
    print(rule())
    skipped = []
    for key, (aws_price, note) in AWS.items():
        entry = prices.get(key, {})
        oci = entry.get("price")
        if oci is None or entry.get("free_tier"):
            # Comparing a paid AWS rate against a free-tier placeholder would
            # flatter OCI for no reason. Leave it out and say so.
            skipped.append(key)
            continue
        oci = float(oci)
        marker = "  <" if oci < aws_price else ("  >" if oci > aws_price else "  =")
        print(f"{key:<18}{money(oci):>12}{money(aws_price):>12}{marker:>4}  {note[:34]}")
    print(rule())
    print("'<' means OCI is cheaper for that unit. List prices, equivalent shapes, no discounts.")
    if skipped:
        print(f"Not compared, no paid rate published: {', '.join(skipped)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gb", type=float, default=1000, help="data volume in GB (default 1000)")
    ap.add_argument("--share", type=int, default=1, help="workloads sharing one cluster")
    ap.add_argument("--hours", type=int, default=20, help="Spark hours per month")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--migrate", default="", help="comma-separated: glue,lambda,iceberg,s3,sqs,...")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    try:
        items = fetch(a.currency)
    except Exception as e:  # noqa: BLE001
        print(f"could not reach Oracle's price list: {e}", file=sys.stderr)
        return 1
    prices = build_prices(items)

    if a.json:
        rows = estimate(prices, a.gb, a.share, a.hours)
        print(json.dumps({
            "prices": prices,
            "estimate": [{"item": n, "basis": b, "cost": c} for n, b, c in rows],
            "total": sum(c for _, _, c in rows),
            "migration": {k: MIGRATION[k] for k in
                          [x.strip().lower() for x in a.migrate.split(",") if x.strip()]
                          if k in MIGRATION},
        }, indent=2))
        return 0

    show_catalogue(prices, a.currency)
    show_estimate(estimate(prices, a.gb, a.share, a.hours), a.share, a.gb)
    show_comparison(prices)
    if a.migrate:
        show_migration(a.migrate.split(","), prices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
