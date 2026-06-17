# How to use this skill folder

Copy `.agent-skills/outbound-payload-lineage` into your repo.

Ask the agent something like:

```text
Use outbound-payload-lineage to trace ContractOfferDispatchRequest sent by DealerPortalClient.sendContractOffer.
```

The agent should create:

```text
lineage-runs/<run-name>/lineage-report.json
lineage-runs/<run-name>/lineage-report.md
lineage-runs/<run-name>/lineage-viewer.html
```

The HTML is self-contained and should open directly in a browser.

To package a viewer manually:

```bash
python .agent-skills/outbound-payload-lineage/scripts/package_viewer.py \
  --json lineage-runs/<run-name>/lineage-report.json \
  --out lineage-runs/<run-name>/lineage-viewer.html
```

To validate the JSON manually:

```bash
python .agent-skills/outbound-payload-lineage/scripts/validate_lineage_report.py \
  lineage-runs/<run-name>/lineage-report.json
```
