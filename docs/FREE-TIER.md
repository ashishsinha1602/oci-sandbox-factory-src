# Free Tier edition

Sandbox Factory installs on an **Oracle Cloud Free Tier account** (no payment method) as the
Free Tier edition. Pick **Edition: free** on the install form. Everything else is the same
one-click stack.

## What is different

| | Standard edition | Free Tier edition |
|---|---|---|
| Account | Pay As You Go or paid | Free Tier (Always Free resources only) |
| Workers | Container Instances (1 to 10) | One Always Free VM (`VM.Standard.E2.1.Micro`) running the same worker image under podman |
| Assistant | OCI Generative AI | Google Gemini API with your own free key from [Google AI Studio](https://aistudio.google.com/apikey); without a key the one-click starters and the form still work |
| Sandboxes can build | Everything | An Always Free Autonomous Database (23ai, REST, in-database document search), NoSQL tables, Object Storage buckets |

## What a Free Tier account cannot have

These are not part of Always Free, so the factory says so instead of failing minutes later:

- Kafka (Streaming with Apache Kafka)
- Containerised apps and container images (Container Instances, API Gateway)
- Functions
- Data Flow (Spark, Iceberg)
- Data Catalog
- Queue
- AI Data Platform
- A second sandbox database (Always Free allows two per tenancy; the factory's control database is one of them)
- Paid database tiers, and Select AI (OCI Generative AI)

The chat declines them with the free alternative. A request that still carries one of them is
refused before anything is built, with the same message on the card.

## Limits to know

- Always Free resources live in your **home region**; install there.
- Always Free capacity is shared; a VM or database can be refused with "out of capacity" at busy
  times. Try again later, it is not a factory error.
- One free database slot is left after the control database, so one database sandbox at a time.
  Destroy it (or let it expire) before creating the next.
- Free Tier tenancies are limited to 2 VCNs; the factory uses one.

## Upgrading later

Upgrade the tenancy to Pay As You Go, then install the standard edition with a new prefix. The
Free Tier install can be destroyed from Resource Manager like any stack.
