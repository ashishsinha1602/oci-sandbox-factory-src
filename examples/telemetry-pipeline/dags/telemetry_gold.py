"""Telemetry pipeline: land raw readings, build the gold tables with Spark,
register them in the catalog. The AWS shape this replaces:

    Airflow (MWAA)  ->  S3 (raw)  ->  Glue job  ->  Iceberg/Athena gold table  ->  Glue Data Catalog

On OCI, task for task:

    Airflow (container instance)  ->  Object Storage (raw/)  ->  Data Flow run of spark/gold_etl.py
                                  ->  Object Storage (gold/)  ->  Data Catalog harvest

The DAG authenticates as the container it runs in (resource principal): no
keys anywhere. Everything it needs comes from the environment the sandbox
injects:

    SANDBOX_ID, SANDBOX_COMPARTMENT_OCID      always
    DATA_BUCKET, OCI_NAMESPACE                the sandbox data bucket
    DATAFLOW_APP_NAME                         the Spark job (default sbx-<id>-gold-etl)
    DATA_CATALOG_NAME                         the catalog (default sbx-<id>-catalog)

It runs once when the scheduler loads it (schedule "@once", unpaused), and
can be re-run from the Airflow UI any time.
"""
from __future__ import annotations

import csv
import io
import math
import os
import random
import time
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

SANDBOX = os.environ.get("SANDBOX_ID", "sandbox")
COMPARTMENT = os.environ.get("SANDBOX_COMPARTMENT_OCID", "")
BUCKET = os.environ.get("DATA_BUCKET", f"sbx-{SANDBOX}-data")
NAMESPACE = os.environ.get("OCI_NAMESPACE", "")
DATAFLOW_APP = os.environ.get("DATAFLOW_APP_NAME", f"sbx-{SANDBOX}-gold-etl")
CATALOG = os.environ.get("DATA_CATALOG_NAME", f"sbx-{SANDBOX}-catalog")


# ---------------------------------------------------------------------------
# OCI clients, as the container itself
# ---------------------------------------------------------------------------
def _auth():
    import oci
    if os.environ.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        signer = oci.auth.signers.get_resource_principals_signer()
        return {"config": {"region": signer.region, "tenancy": signer.tenancy_id}, "signer": signer}
    return {"config": oci.config.from_file()}


def _client(cls):
    return cls(**_auth())


def _region():
    return _auth()["config"]["region"]


# ---------------------------------------------------------------------------
# 1. raw: synthetic device readings, the way a fleet would send them
# ---------------------------------------------------------------------------
def land_raw_readings(**ctx):
    import oci
    os_client = _client(oci.object_storage.ObjectStorageClient)
    ns = NAMESPACE or os_client.get_namespace().data
    random.seed(42)
    sites = {"phx-1": ["dev-001", "dev-002", "dev-003"], "ams-2": ["dev-101", "dev-102"], "syd-3": ["dev-201"]}
    start = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
    files = 0
    for day in range(2):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["device_id", "site", "ts", "temperature_c", "humidity_pct", "battery_pct", "event"])
        for site, devices in sites.items():
            for dev in devices:
                battery = 100.0 - random.random() * 30
                base_t = 20 + hash(dev) % 8
                t = start + timedelta(days=day)
                while t < start + timedelta(days=day + 1):
                    # a device sends every 2 minutes, sleeps for 15-40 minutes a few times a day
                    if random.random() < 0.02:
                        t += timedelta(minutes=random.randint(15, 40))
                        continue
                    hour = t.hour + t.minute / 60
                    temp = base_t + 4 * math.sin((hour - 6) / 24 * 2 * math.pi) + random.gauss(0, 0.4)
                    event = "reading"
                    if random.random() < 0.004:          # a spike the z-score should catch
                        temp += random.choice([-1, 1]) * random.uniform(6, 12)
                    if random.random() < 0.001:          # a glitch the cleaner should null out
                        temp = 999.0
                    if dev == "dev-102" and random.random() < 0.003:
                        event = "fault"
                    battery = max(3.0, battery - 0.02)
                    row = [dev, site, t.strftime("%Y-%m-%d %H:%M:%S"), round(temp, 2),
                           round(45 + random.gauss(0, 5), 1), round(battery, 1), event]
                    w.writerow(row)
                    if random.random() < 0.01:           # retransmit: same reading twice
                        w.writerow(row)
                    t += timedelta(minutes=2)
        name = f"raw/readings_{(start + timedelta(days=day)).date()}.csv"
        os_client.put_object(ns, BUCKET, name, buf.getvalue().encode())
        files += 1
    print(f"landed {files} raw files in oci://{BUCKET}@{ns}/raw/")
    return ns


# ---------------------------------------------------------------------------
# 2. Spark on Data Flow: the gold tables
# ---------------------------------------------------------------------------
def run_gold_etl(**ctx):
    import oci
    df = _client(oci.data_flow.DataFlowClient)
    ns = ctx["ti"].xcom_pull(task_ids="land_raw_readings") or NAMESPACE
    apps = [a for a in df.list_applications(COMPARTMENT, display_name=DATAFLOW_APP).data
            if a.lifecycle_state == "ACTIVE"]
    if not apps:
        raise RuntimeError(f"Data Flow application {DATAFLOW_APP} not found in the sandbox compartment")
    run = df.create_run(oci.data_flow.models.CreateRunDetails(
        compartment_id=COMPARTMENT, application_id=apps[0].id,
        display_name=f"gold-etl {datetime.utcnow():%Y-%m-%d %H:%M}",
        arguments=["--bucket", BUCKET, "--namespace", ns])).data
    print(f"Data Flow run {run.id} started")
    deadline = time.time() + 45 * 60
    while time.time() < deadline:
        r = df.get_run(run.id).data
        if r.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED", "STOPPED"):
            print(f"run finished: {r.lifecycle_state} {r.lifecycle_details or ''}")
            if r.lifecycle_state != "SUCCEEDED":
                raise RuntimeError(f"Data Flow run {r.lifecycle_state}: {r.lifecycle_details}")
            return run.id
        time.sleep(30)
    raise TimeoutError("Data Flow run did not finish in 45 minutes")


# ---------------------------------------------------------------------------
# 3. Data Catalog: register the bucket and harvest, so the gold tables show up
# ---------------------------------------------------------------------------
def harvest_catalog(**ctx):
    import oci
    dc = _client(oci.data_catalog.DataCatalogClient)
    ns = ctx["ti"].xcom_pull(task_ids="land_raw_readings") or NAMESPACE
    cats = [c for c in dc.list_catalogs(compartment_id=COMPARTMENT, display_name=CATALOG).data
            if c.lifecycle_state == "ACTIVE"]
    if not cats:
        raise RuntimeError(f"Data Catalog {CATALOG} not found in the sandbox compartment")
    cid = cats[0].id
    types = {(t.type_category, t.name): t.key for t in dc.list_types(cid, limit=500).data.items}
    asset_type = types[("dataAsset", "Oracle Object Storage")]
    conn_type = next(v for (cat, name), v in types.items() if cat == "connection" and name == "Resource Principal"
                     and dc.get_type(cid, v).data.parent_type_name == "Oracle Object Storage")

    assets = [a for a in dc.list_data_assets(cid, display_name=BUCKET).data.items]
    if assets:
        asset_key = assets[0].key
    else:
        asset_key = dc.create_data_asset(cid, oci.data_catalog.models.CreateDataAssetDetails(
            display_name=BUCKET, type_key=asset_type,
            description=f"Sandbox {SANDBOX} data bucket: raw/ and gold/",
            properties={"default": {"url": f"https://objectstorage.{_region()}.oraclecloud.com", "namespace": ns}})).data.key
    conns = dc.list_connections(cid, asset_key).data.items
    if conns:
        conn_key = conns[0].key
    else:
        conn_key = dc.create_connection(cid, asset_key, oci.data_catalog.models.CreateConnectionDetails(
            display_name="resource-principal", type_key=conn_type, is_default=True,
            properties={"default": {"ociRegion": _region(), "ociCompartment": COMPARTMENT}})).data.key

    jds = dc.list_job_definitions(cid, data_asset_key=asset_key, display_name="harvest-gold").data.items
    if jds:
        jd_key = jds[0].key
    else:
        jd_key = dc.create_job_definition(cid, oci.data_catalog.models.CreateJobDefinitionDetails(
            display_name="harvest-gold", job_type="HARVEST", data_asset_key=asset_key,
            connection_key=conn_key, is_incremental=False, is_sample_data_extracted=False)).data.key
    jobs = dc.list_jobs(cid, job_definition_key=jd_key).data.items
    job_key = jobs[0].key if jobs else dc.create_job(cid, oci.data_catalog.models.CreateJobDetails(
        display_name="harvest-gold", job_definition_key=jd_key)).data.key
    ex = dc.create_job_execution(cid, job_key, oci.data_catalog.models.CreateJobExecutionDetails(job_type="HARVEST")).data
    print(f"harvest started: job {job_key} execution {ex.key}")
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        e = dc.get_job_execution(cid, job_key, ex.key).data
        if e.lifecycle_state in ("SUCCEEDED", "FAILED", "CANCELED", "SUCCEEDED_WITH_WARNINGS"):
            print(f"harvest {e.lifecycle_state}: {e.error_message or ''}")
            if e.lifecycle_state == "FAILED":
                raise RuntimeError(f"harvest failed: {e.error_code} {e.error_message}")
            return e.key
        time.sleep(20)
    raise TimeoutError("harvest did not finish in 20 minutes")


with DAG(
    dag_id="telemetry_gold",
    description="raw readings -> Spark gold tables -> Data Catalog",
    schedule="@once",
    start_date=datetime(2024, 1, 1),
    catchup=True,
    is_paused_upon_creation=False,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=2)},
    tags=["telemetry", "gold", "data-flow", "data-catalog"],
) as dag:
    land = PythonOperator(task_id="land_raw_readings", python_callable=land_raw_readings)
    etl = PythonOperator(task_id="run_gold_etl", python_callable=run_gold_etl)
    harvest = PythonOperator(task_id="harvest_catalog", python_callable=harvest_catalog)
    land >> etl >> harvest
