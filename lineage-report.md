# ContractOfferDispatchRequest Lineage Report

Object traced: `ContractOfferDispatchRequest`

Outbound surface: `DealerPortalClient.sendContractOffer(...)` at `src/main/java/com/acme/vendor/DealerPortalClient.java:88`

Fields found: 18

Confidence summary:

- High: 12
- Medium: 5
- Low: 1

Primary origins:

- Kafka `ApplicationApprovedEvent`
- `ContractRepository`
- `ApplicantRepository`
- `IncomeVerificationClient`
- `DecisionEngineResult`
- `application.yml`

## Important notes

- Derived fields expand into their inputs, for example `applicant.fullName` traces first, middle, last name, and delimiter separately.
- `financialTerms.paymentToIncomeRatio` traces both monthly payment and income verification inputs.
- `metadata.internalNotes` is low confidence because dynamic copy logic prevents a complete static trace.

Open `lineage-viewer.html` for the interactive report.
